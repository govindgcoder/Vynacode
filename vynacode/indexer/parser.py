import tree_sitter_python as tspython
from schema import Block, Parameter
from tree_sitter import Language, Parser

# for python
PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)

query_string = """
(class_definition) @class
(function_definition) @function
"""
query = PY_LANGUAGE.query(query_string)

import uuid


def parse_python_file(filepath: Path) -> List[Block]:
    with open(filepath, "rb") as f:
        tree = parser.parse(f.read())
        block_list = []
        for node, tag in query.captures(tree.root_node):
            name_node = node.child_by_field_name("name")
            if not name_node:
                continue
            name = name_node.text.decode("utf-8")
            str_val = f"{filepath}:{name}:{node.start_byte}"
            id_val = hashlib.sha256(str_val.encode()).hexdigest()
            type_val = tag
            start_byte = node.start_byte
            end_byte = node.end_byte
            line_range = (node.start_point[0], node.end_point[0])
            block_list.append(
                Block(
                    id=id_val,
                    name=name,
                    type=type_val,
                    byte_start=start_byte,
                    byte_end=end_byte,
                    line_range=line_range,
                )
            )
        return block_list
