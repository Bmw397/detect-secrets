import base64
import re
from collections import deque
from functools import lru_cache
from typing import Any
from typing import cast
from typing import Dict
from typing import Iterator
from typing import List
from typing import NamedTuple
from typing import Optional
from typing import Pattern
from typing import Tuple
from typing import Union

import yaml
from yaml.tokens import FlowEntryToken
from yaml.tokens import KeyToken

from ..types import NamedIO
from ..util.filetype import determine_file_type
from ..util.filetype import FileType
from .base import BaseTransformer
from .exceptions import ParsingError


class YAMLTransformer(BaseTransformer):
    def should_parse_file(self, filename: str) -> bool:
        return determine_file_type(filename) == FileType.YAML

    def parse_file(self, file: NamedIO) -> List[str]:
        """
        :raises: ParsingError
        """
        try:
            items = sorted(YAMLFileParser(file), key=lambda x: x.line_number)
        except yaml.YAMLError:
            raise ParsingError

        lines: List[str] = []
        for item in items:
            while len(lines) < item.line_number - 1:
                lines.append('')

            value = item.value
            if isinstance(value, bytes):
                # binary strings in YAML are base64 encoded. https://yaml.org/type/binary.html
                # While the YAML parser already decodes it for us, we want to capture the *raw*
                # base64 encoded value for two reasons:
                #   1. Increases coverage
                #      Our Base64HighEntropyString plugin is tuned for base64 strings. Including
                #      other potential characters excludes it from this scan, with no real
                #      potential gain. While the entropy limit may be different, the fact that
                #      the string is processable is a win already.
                #
                #   2. Supports audit functionality
                #      When we convert this value to its unicode representation, we need to
                #      performs several hacks in order to be able to find the raw binary string
                #      again, during our audit process. Keeping it to its original value simplifies
                #      this process.
                value = base64.b64encode(value).decode()

            line = item.line.strip()
            # TODO: parse the difference between block_scalar styles, and handle appropriately.
            # See test cases for more details.

            comment = ''
            matches = _yaml_comment_regex().search(line)
            if matches:
                comment = matches.group(0)

            # YAML does the parsing for us, and the value we'll receive is a string proper
            # (rather than the raw value, like `ConfigFileTransformer`). We want to artifically
            # add quotes here since we know they are strings, HighEntropyString will benefit
            # from this, and all other plugins don't care.
            #
            # However, if there is a quote inside, we need to escape it.
            value = value.replace('"', '\\"')

            lines.append(f'{item.key}: "{value}"{comment}')     # type: ignore

        return lines


@lru_cache(maxsize=1)
def _yaml_comment_regex() -> Pattern:
    """
    From https://yaml-multiline.info/, it states that `#` cannot appear *after* a space
    or a newline, otherwise it will be a syntax error (for multiline strings that don't
    use a block scalar). This applies to single lines as well: for example, `a#b` will be
    treated as a single value, but `a #b` will only capture `a`, leaving `#b` as a comment.

    For lines that *do* use a block scalar, the YAML parser will throw a syntax error if
    there is additional text on the same line as the block scalar. Comments however, are fine.
    e.g.
        key: | # this is ok
        blah

        key: | but this is not
        blah

    Given that we've made it to this stage, we can assume the YAML file is syntactically
    correct. Therefore, if we add whitespace before the comment character, we can know that
    everything else *after* the comment character is a comment for a given line.
    """
    return re.compile(r'(\s+#[\S ]*)')


class YAMLValue(NamedTuple):
    key: str
    value: Union[str, bytes]
    line_number: int
    line: str


class YAMLFileParser:
    """
    Yaml config files are interesting, because they don't necessarily conform
    to our basic regex for detecting HighEntropyStrings as strings don't
    need to be quoted.

    This causes interesting issues, because our regex won't catch non-quoted
    strings, and if we ignore the quoting requirement, then we increase our
    false positive rate, because any long string would have high entropy.

    Therefore, we take a different approach: intercept the parsing of the yaml
    file to identify string values. This assumes:

        1. Secrets are strings or binaries
        2. Secrets are not keys

    Then, we calculate the entropy of those string values.

    The difficulty comes from determining the line number which these values
    come from. To do this, we transform the string into a dictionary of
    meta-tags, in the following format:

    >>> {
        'key': {
            '__value__': value,
            '__line__': <line_number>,
        }
    }

    This way, we can quickly identify the line number for auditing at a later
    stage.

    This parsing method is inspired by https://stackoverflow.com/a/13319530.
    """

    def __init__(self, file: NamedIO):
        self.content = file.read()

        self.loader = yaml.SafeLoader(self.content)
        self.loader.compose_node = self._compose_node_shim  # type: ignore

        self.is_inline_flow_mapping_key = False
        self.loader.parse_flow_mapping_key = self._parse_flow_mapping_key_shim  # type: ignore

    def json(self) -> Dict[str, Any]:
        return cast(Dict[str, Any], self.loader.get_single_data())

    def __iter__(self) -> Iterator[YAMLValue]:
        """
        :returns: (value, line_number)
        """
        # Used to obtain inline comments
        lines = self.content.splitlines()

        to_search = deque([self.json()])
        while to_search:
            item: Any = to_search.popleft()

            if not item:
                # mainly for base case (e.g. if file is all comments)
                continue

            # If it doesn't have our meta-tags, it's not a value worth scanning.
            try:
                if '__line__' not in item:
                    if isinstance(item, str):
                        continue

                    # However, we need to recursively search in the dictionary for other such values
                    # that we may care about.
                    try:
                        to_search.extend(item.values())
                    except AttributeError:
                        # This is an array
                        to_search.extend(item)

                    continue
            except TypeError:
                # e.g. if item is a float.
                continue

            yield YAMLValue(
                key=item['__original_key__'],
                value=item['__value__'],
                line_number=item['__line__'],

                # We extract this separately because the parser drops the comments
                # (at least up to version 3.13).
                # https://github.com/yaml/pyyaml/blob/a2d481b8dbd2b352cb001f07091ccf669227290f/lib3/yaml/scanner.py#L749
                # The line value feeds into the filters, and helps us tune false positives.
                line=lines[item['__line__'] - 1],
            )

    def _compose_node_shim(
        self,
        parent: Optional[yaml.nodes.Node],
        index: Optional[yaml.nodes.Node],
    ) -> yaml.nodes.Node:
        line = (
            self.loader.marks[-1].line
            if self.is_inline_flow_mapping_key
            else self.loader.line
        )

        node = yaml.composer.Composer.compose_node(self.loader, parent, index)
        node.__line__ = line + 1

        if node.tag.endswith(':map'):
            # Reset the inline flow mapping key when the end of a mapping is reached
            # to avoid complications with empty mappings
            self.is_inline_flow_mapping_key = False
            return _tag_dict_values(node)

        # TODO: Not sure if need to do :seq

        return cast(yaml.nodes.Node, node)

    def _parse_flow_mapping_key_shim(
        self,
        first: bool = False,
    ) -> yaml.nodes.Node:
        # There exists an edge case when a key and flow mapping start character `{` are on the same
        # line (Ex. '{key: value}) followed by an empty line. The parser will produce an off-by-one
        # error for the line number that it tracks internally. Since we track the start of the
        # mapping, we will use this line number when we are processing:
        # A) The first key in an inline dictionary where the flow mapping start character is on the
        # same line as the key
        # B) The n key of an inline dictionary that is followed by a FlowEntryToken (',') and
        # KeyToken ('key:')
        is_inline_dictionary = (
            first
            and self.loader.marks[-1].line == self.loader.peek_token().start_mark.line
            or self._check_next_tokens_shim(FlowEntryToken, KeyToken)
        )

        if is_inline_dictionary:
            self.is_inline_flow_mapping_key = True
        else:
            self.is_inline_flow_mapping_key = False

        return cast(yaml.nodes.Node, yaml.parser.Parser.parse_flow_mapping_key(self.loader, first))

    def _check_next_tokens_shim(
        self,
        *choices: Any,
    ) -> bool:
        """Check the next tokens type match the argument list of token types."""
        result = True
        i = 0

        if self.loader.tokens:
            if not choices:
                return result
            for choice in choices:
                if i < len(self.loader.tokens):
                    result = result and isinstance(self.loader.tokens[i], choice)
                    i += 1
        else:
            result = False

        return result


def _tag_dict_values(map_node: yaml.nodes.MappingNode) -> yaml.nodes.MappingNode:
    """
    :param map_node: It looks like map_node.value contains a list of
        pair tuples, corresponding to key,value pairs.
    """
    new_values = []
    for key, value in map_node.value:
        if not (
            value.tag.endswith(':str') or
            value.tag.endswith(':binary')
        ):
            new_values.append((key, value))
            continue

        augmented_string = yaml.nodes.MappingNode(
            tag=map_node.tag,
            value=[
                _create_key_value_pair_for_mapping_node_value(
                    key='__value__',
                    value=value.value,
                    tag=value.tag,
                ),
                _create_key_value_pair_for_mapping_node_value(
                    key='__line__',
                    value=str(value.__line__),
                    tag='tag:yaml.org,2002:int',
                ),
                _create_key_value_pair_for_mapping_node_value(
                    key='__original_key__',
                    value=key.value,
                    tag='tag:yaml.org,2002:str',
                ),
            ],
        )

        new_values.append((key, augmented_string))

    output = yaml.nodes.MappingNode(
        tag=map_node.tag,
        value=new_values,
        start_mark=map_node.start_mark,
        end_mark=map_node.end_mark,
        flow_style=map_node.flow_style,
    )
    return output


def _create_key_value_pair_for_mapping_node_value(
    key: str,
    value: Any,
    tag: str,
) -> Tuple[yaml.nodes.ScalarNode, yaml.nodes.ScalarNode]:
    return (
        yaml.nodes.ScalarNode(
            tag='tag:yaml.org,2002:str',
            value=key,
        ),
        yaml.nodes.ScalarNode(
            tag=tag,
            value=value,
        ),
    )
