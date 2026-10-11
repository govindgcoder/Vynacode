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
from .schema import Block
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

# Synthetic module blocks cap: imports and module constants are short; a wall
# of them still must not become one block that owns the token budget.
_MODULE_CHUNK_LINES = 60


def _module_blocks(filepath: Path, source: bytes, definitions: List[Block]) -> List[Block]:
    """Synthetic blocks for the line spans no definition covers.

    Imports, module constants and prompt strings match no tree-sitter
    definition query, so without these the lines holding e.g.
    MAX_RETRIES = 3 are invisible to FTS, to summaries and to the anchor
    editor — the exact failure that made "change MAX_RETRIES" edits
    impossible to retrieve. Nested definitions (methods inside a class)
    overlap their parent; the merged coverage union absorbs them.
    """
    if not source:
        return []
    covered = sorted((b.line_range for b in definitions))
    merged: List[tuple[int, int]] = []
    for start, end in covered:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))

    # 1-indexed line starts; a trailing newline appends a phantom start at
    # EOF that is not a real line, dropped by the endswith check below.
    line_starts = [0, 0]
    for i, byte in enumerate(source):
        if byte == 0x0A:
            line_starts.append(i + 1)
    total_lines = len(line_starts) - 1 - (1 if source.endswith(b"\n") else 0)

    gaps: List[tuple[int, int]] = []
    cursor = 1
    for start, end in merged:
        if start > cursor:
            gaps.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= total_lines:
        gaps.append((cursor, total_lines))

    blocks: List[Block] = []
    for gap_start, gap_end in gaps:
        chunk_start = gap_start
        while chunk_start <= gap_end:
            chunk_end = min(chunk_start + _MODULE_CHUNK_LINES - 1, gap_end)
            byte_start = line_starts[chunk_start]
            byte_end = (line_starts[chunk_end + 1]
                        if chunk_end + 1 < len(line_starts) else len(source))
            text = source[byte_start:byte_end]
            if not text.strip():
                # A gap of pure whitespace separates definitions; indexing it
                # would only add empty blocks to FTS.
                chunk_start = chunk_end + 1
                continue
            header = next(
                (ln.strip() for ln in text.splitlines() if ln.strip()), ""
            )
            id_val = hashlib.sha256(
                f"{filepath}:module:{chunk_start}".encode()
            ).hexdigest()
            blocks.append(
                Block(
                    id=id_val,
                    name=filepath.stem,
                    type="module",
                    signature=header[:_SIG_MAX],
                    line_range=(chunk_start, chunk_end),
                    byte_start=byte_start,
                    byte_end=byte_end,
                    parent_id=None,
                    parent_file=str(filepath),
                )
            )
            chunk_start = chunk_end + 1
    return blocks


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
    block_list.extend(_module_blocks(filepath, source, block_list))
    block_list.sort(key=lambda b: b.byte_start)
    return block_list
