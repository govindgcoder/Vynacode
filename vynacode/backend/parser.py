import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import List

import tree_sitter_c_sharp as tscsharp
import tree_sitter_go as tsgo
import tree_sitter_java as tsjava
import tree_sitter_javascript as tsjavascript
import tree_sitter_kotlin as tskotlin
import tree_sitter_python as tspython
import tree_sitter_rust as tsrust
import tree_sitter_swift as tsswift
import tree_sitter_typescript as tstypescript
from schema import Block
from tree_sitter import Language, Parser, Query, QueryCursor


@dataclass(frozen=True)
class LangSpec:
    suffixes: tuple[str, ...]
    language: Language
    query: Query


# One query string per language. Captures are grouped by role; the tag becomes
# Block.type. Node names were checked against each grammar's node-types.
_REGISTRY = [
    (
        (".py",),
        Language(tspython.language()),
        "(class_definition) @class\n(function_definition) @function",
    ),
    (
        (".js", ".jsx", ".mjs", ".cjs"),
        Language(tsjavascript.language()),
        "(function_declaration) @function\n"
        "(class_declaration) @class\n"
        "(method_definition) @method",
    ),
    (
        (".ts",),
        Language(tstypescript.language_typescript()),
        "(function_declaration) @function\n"
        "(class_declaration) @class\n"
        "(method_definition) @method\n"
        "(interface_declaration) @interface",
    ),
    (
        (".tsx",),
        Language(tstypescript.language_tsx()),
        "(function_declaration) @function\n"
        "(class_declaration) @class\n"
        "(method_definition) @method\n"
        "(interface_declaration) @interface",
    ),
    (
        (".go",),
        Language(tsgo.language()),
        "(function_declaration) @function\n"
        "(method_declaration) @method\n"
        "(type_spec) @class",
    ),
    (
        (".rs",),
        Language(tsrust.language()),
        "(function_item) @function\n"
        "(struct_item) @class\n"
        "(enum_item) @class\n"
        "(trait_item) @interface",
    ),
    (
        (".java",),
        Language(tsjava.language()),
        "(class_declaration) @class\n"
        "(interface_declaration) @interface\n"
        "(method_declaration) @method\n"
        "(constructor_declaration) @method",
    ),
    (
        (".cs",),
        Language(tscsharp.language()),
        "(class_declaration) @class\n"
        "(interface_declaration) @interface\n"
        "(struct_declaration) @class\n"
        "(enum_declaration) @class\n"
        "(record_declaration) @class\n"
        "(method_declaration) @method\n"
        "(constructor_declaration) @method",
    ),
    (
        (".kt", ".kts"),
        Language(tskotlin.language()),
        "(class_declaration) @class\n"
        "(object_declaration) @class\n"
        "(function_declaration) @function",
    ),
    (
        (".swift",),
        Language(tsswift.language()),
        "(class_declaration) @class\n"
        "(protocol_declaration) @interface\n"
        "(function_declaration) @function\n"
        "(protocol_function_declaration) @method",
    ),
]

LANGUAGES: dict[str, LangSpec] = {}
for _suffixes, _language, _query_string in _REGISTRY:
    _spec = LangSpec(_suffixes, _language, Query(_language, _query_string))
    for _suffix in _suffixes:
        LANGUAGES[_suffix] = _spec

SUPPORTED_SUFFIXES = frozenset(LANGUAGES)

PARSER = Parser()

# Signatures are prompt hints, not data: a few hundred chars keeps Go structs
# and generic Rust headers useful without letting one block eat the budget.
_SIG_MAX = 200


def _signature(node, source: bytes) -> str:
    """The definition's header text, from its first byte up to its body.
    """
    body = node.child_by_field_name("body")
    if body is None:
        body = next((c for c in node.children if c.type.endswith("_body")), None)
    end = body.start_byte if body else node.end_byte
    text = source[node.start_byte:end].decode("utf-8", errors="replace")
    return " ".join(text.split())[:_SIG_MAX]


def parse_file(filepath: Path) -> List[Block]:
    """Parse any registered language into blocks, or [] if the suffix is unknown."""
    spec = LANGUAGES.get(filepath.suffix.lower())
    if spec is None:
        return []
    source = filepath.read_bytes()
    PARSER.language = spec.language
    tree = PARSER.parse(source)
    block_list = []
    captures = QueryCursor(spec.query).captures(tree.root_node)
    for tag, nodes in captures.items():
        for node in nodes:
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            name = name_node.text.decode("utf-8")
            str_val = f"{filepath}:{name}:{node.start_byte}"
            id_val = hashlib.sha256(str_val.encode()).hexdigest()
            # tree-sitter rows are 0-indexed, but unified diff hunks and every
            # editor are 1-indexed, and the line numbers go straight into the
            # prompt for the model to patch against. Store 1-indexed so
            # downstream never has to remember the offset.
            # end_point[0] is the row of the LAST line, inclusive.
            line_range = (node.start_point[0] + 1, node.end_point[0] + 1)
            # A method nested in class_body/block has no named parent, so
            # parent_id stays None rather than inventing an "unknown" node.
            parent_name_node = node.parent.child_by_field_name("name")
            if parent_name_node is None:
                id_parent = None
            else:
                parent_name = parent_name_node.text.decode("utf-8")
                str_val_parent = f"{filepath}:{parent_name}:{node.parent.start_byte}"
                id_parent = hashlib.sha256(str_val_parent.encode()).hexdigest()
            block_list.append(
                Block(
                    id=id_val,
                    name=name,
                    type=tag,
                    signature=_signature(node, source),
                    byte_start=node.start_byte,
                    byte_end=node.end_byte,
                    line_range=line_range,
                    parent_id=id_parent,
                    parent_file=str(filepath),
                ),
            )
    # Captures arrive grouped by tag; byte order is what the prompt and anchor
    # logic assume, so restore it.
    block_list.sort(key=lambda b: b.byte_start)
    return block_list
