import hashlib
from pathlib import Path

chunk_size = 65536


def compute_sha256(filepath: Path):
    sha256_computer = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            sha256_computer.update(chunk)
    return sha256_computer.hexdigest()


import os

import pathspec
from schema import FileMetaData

# Applied always, whether or not a project defines .vynaignore. Kept in code
# rather than written to a file so non-interactive runs (CI) get the same sane
# default; a project-local file only has to add its own exceptions.
BUILTIN_IGNORES = [
    "__pycache__/",
    "*.py[cod]",
    ".venv/",
    "build/",
    "dist/",
    ".vynarc",
    ".python-version",
]


def build_pathspec(base: Path):
    lines = [".git/", ".vc/", "codebase.json", "codebase.md", *BUILTIN_IGNORES]
    for file in (".gitignore", ".vynaignore"):
        path = base / file
        try:
            with open(path, "r") as f:
                vals = f.readlines()
                val = [val.rstrip() for val in vals]
                lines.extend(val)
        except FileNotFoundError:
            pass

    return pathspec.PathSpec.from_lines("gitignore", lines)


def walk(base: Path):
    spec = build_pathspec(base)
    for root, dirs, files in os.walk(base):
        root_path = Path(root)
        rel_root = root_path.relative_to(base)
        dirs[:] = [d for d in dirs if not spec.match_file(str(rel_root / d) + "/")]
        for f in files:
            rel_file_path = rel_root / f
            if not spec.match_file(str(rel_file_path)):
                path = base / rel_file_path
                yield FileMetaData(
                    name=path.name,
                    path=str(path),
                    hash=compute_sha256(path),
                    size_bytes=path.stat().st_size,
                    language=path.suffix,
                )
