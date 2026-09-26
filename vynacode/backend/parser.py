import hashlib
from pathlib import Path
from typing import List

import tree_sitter_python as tspython
from schema import Block, Parameter
from tree_sitter import Language, Parser, Query, QueryCursor

# for python
PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

query_string = """
(class_definition) @class
(function_definition) @function
"""
query = Query(PY_LANGUAGE, query_string)

import uuid


# tree-sitter-python emits these node types for real parameters. The
# separators '/' and '*' are also named children of `parameters`, so an
# allowlist is required or they would be indexed as parameters named "/" and "*".
PARAM_NODE_TYPES = {
    "identifier",
    "typed_parameter",
    "typed_default_parameter",
    "default_parameter",
    "list_splat_pattern",
    "dictionary_splat_pattern",
}


def extract_params(node) -> List[Parameter]:
    params = []
    params_node = node.child_by_field_name("parameters")
    if not params_node:
        return []
    for child in params_node.named_children:
        if child.type not in PARAM_NODE_TYPES:
            continue
        # Name resolution is uniform across all six shapes: annotated and
        # defaulted forms expose a `name` field, splats and typed params expose
        # the identifier as their first named child, and a bare parameter is a
        # leaf whose own text is the identifier.
        name_node = child.child_by_field_name("name")
        if name_node is None:
            named = child.named_children
            name_node = named[0] if named else child
        type_node = child.child_by_field_name("type")
        params.append(
            Parameter(
                name=name_node.text.decode("utf-8"),
                annotation=type_node.text.decode("utf-8") if type_node else None,
            )
        )
    return params


def parse_python_file(filepath: Path) -> List[Block]:
    with open(filepath, "rb") as f:
        tree = parser.parse(f.read())
        block_list = []
        captures = QueryCursor(query).captures(tree.root_node)
        for tag, nodes in captures.items():
            for node in nodes:
                name_node = node.child_by_field_name("name")
                if not name_node:
                    continue
                name = name_node.text.decode("utf-8")
                str_val = f"{filepath}:{name}:{node.start_byte}"
                id_val = hashlib.sha256(str_val.encode()).hexdigest()
                type_val = tag
                start_byte = node.start_byte
                end_byte = node.end_byte
                # tree-sitter rows are 0-indexed, but unified diff hunks and every
                # editor are 1-indexed, and the line numbers go straight into the
                # prompt for the model to patch against. Store 1-indexed so
                # downstream never has to remember the offset.
                # end_point[0] is the row of the LAST line, inclusive.
                line_range = (node.start_point[0] + 1, node.end_point[0] + 1)
                name_parent_node = node.parent.child_by_field_name("name")
                if node.parent.type == "module":
                    id_parent = None
                else:
                    name_parent = name_parent_node.text.decode("utf-8") if name_parent_node else "unknown"
                    str_val_parent = f"{filepath}:{name_parent}:{node.parent.start_byte}"
                    id_parent = hashlib.sha256(str_val_parent.encode()).hexdigest()
                return_node = node.child_by_field_name("return_type")
                returns_val = return_node.text.decode("utf-8") if return_node else None
                block_list.append(
                    Block(
                        id=id_val,
                        name=name,
                        type=type_val,
                        params=extract_params(node),
                        byte_start=start_byte,
                        byte_end=end_byte,
                        line_range=line_range,
                        is_async=any(
                            c.type == "async" for c in node.children
                        ),
                        returns=returns_val,
                        parent_id=id_parent,
                        parent_file=str(filepath)
                    ),
                )
        return block_list
