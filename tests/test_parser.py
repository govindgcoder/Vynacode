"""Multi-language block extraction. Run: .venv/bin/pytest tests/test_parser.py -v"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
# The backend modules import each other bare (e.g. `from schema import Block`),
# exactly as cli.py arranges, so the backend dir must be importable.
sys.path.insert(0, str(ROOT / "vynacode" / "backend"))

from parser import SUPPORTED_SUFFIXES, parse_file  # noqa: E402


def _blocks(tmp_path: Path, name: str, source: str):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return parse_file(path)


def _shape(blocks):
    return [(b.name, b.type, b.line_range) for b in blocks]


PY = "def foo(a: int) -> str:\n    return str(a)\n\nclass C(B):\n    def m(self):\n        pass\n"
JS = "function foo(a, b) { return a; }\nclass C { m(x) { return x; } }\n"
TS = "function foo(a: number): string { return ''; }\nclass C { m(x: number): void {} }\ninterface I { x: number; }\n"
GO = "package p\nfunc Foo(a int, b string) error { return nil }\nfunc (s *S) Bar(ctx Ctx) (int, error) { return 0, nil }\ntype S struct { X int }\n"
RS = "fn foo(a: i32) -> i32 { a }\nstruct S { x: i32 }\nenum E { A }\ntrait T { fn m(&self); }\n"
JAVA = "class C { C() {} int m(int a) { return a; } }\ninterface I { void f(); }\n"
CS = 'class C {\n    int M(int a) {\n        return a;\n    }\n    C() {}\n}\ninterface I {\n    void F();\n}\nstruct S { int X; }\nenum E { A }\nrecord R(int X);\n'
KOTLIN = 'class C {\n    fun m(a: Int): Int {\n        return a\n    }\n}\n\nfun foo(a: Int): Int {\n    return a\n}\n\ninterface I {\n    fun f()\n}\n\nobject O {\n    fun g() {}\n}\n'
SWIFT = 'class C {\n    func m(a: Int) -> Int {\n        return a\n    }\n}\n\nstruct S {\n    var x: Int\n}\n\nfunc foo(a: Int) -> Int {\n    return a\n}\n\nprotocol P {\n    func f()\n}\n\nenum E {\n    case a\n}\n'


def test_python_blocks(tmp_path):
    assert _shape(_blocks(tmp_path, "m.py", PY)) == [
        ("foo", "function", (1, 2)),
        ("C", "class", (4, 6)),
        ("m", "function", (5, 6)),
    ]


def test_javascript_blocks(tmp_path):
    assert _shape(_blocks(tmp_path, "m.js", JS)) == [
        ("foo", "function", (1, 1)),
        ("C", "class", (2, 2)),
        ("m", "method", (2, 2)),
    ]


def test_typescript_blocks(tmp_path):
    assert _shape(_blocks(tmp_path, "m.ts", TS)) == [
        ("foo", "function", (1, 1)),
        ("C", "class", (2, 2)),
        ("m", "method", (2, 2)),
        ("I", "interface", (3, 3)),
    ]


def test_tsx_uses_tsx_grammar(tmp_path):
    assert _shape(_blocks(tmp_path, "m.tsx", TS)) == _shape(_blocks(tmp_path, "m.ts", TS))


def test_go_blocks(tmp_path):
    assert _shape(_blocks(tmp_path, "m.go", GO)) == [
        ("Foo", "function", (2, 2)),
        ("Bar", "method", (3, 2 + 1)),
        ("S", "class", (4, 4)),
    ]


def test_rust_blocks(tmp_path):
    assert _shape(_blocks(tmp_path, "m.rs", RS)) == [
        ("foo", "function", (1, 1)),
        ("S", "class", (2, 2)),
        ("E", "class", (3, 3)),
        ("T", "interface", (4, 4)),
    ]


def test_java_blocks(tmp_path):
    assert _shape(_blocks(tmp_path, "m.java", JAVA)) == [
        ("C", "class", (1, 1)),
        ("C", "method", (1, 1)),
        ("m", "method", (1, 1)),
        ("I", "interface", (2, 2)),
        ("f", "method", (2, 2)),
    ]


def test_csharp_blocks(tmp_path):
    assert _shape(_blocks(tmp_path, "m.cs", CS)) == [
        ("C", "class", (1, 6)),
        ("M", "method", (2, 4)),
        ("C", "method", (5, 5)),
        ("I", "interface", (7, 9)),
        ("F", "method", (8, 8)),
        ("S", "class", (10, 10)),
        ("E", "class", (11, 11)),
        ("R", "class", (12, 12)),
    ]


def test_kotlin_blocks(tmp_path):
    # interface/object collapse to @class: the Kotlin grammar has no separate
    # nodes for them, and methods are plain function_declarations.
    assert _shape(_blocks(tmp_path, "m.kt", KOTLIN)) == [
        ("C", "class", (1, 5)),
        ("m", "function", (2, 4)),
        ("foo", "function", (7, 9)),
        ("I", "class", (11, 13)),
        ("f", "function", (12, 12)),
        ("O", "class", (15, 17)),
        ("g", "function", (16, 16)),
    ]


def test_kotlin_signature_uses_body_child(tmp_path):
    # Kotlin exposes no `body` field; the signature must still stop at the
    # header via the `*_body` child fallback, not swallow the whole block.
    blocks = {b.name: b for b in _blocks(tmp_path, "m.kt", KOTLIN)}
    assert blocks["C"].signature == "class C"
    assert blocks["m"].signature == "fun m(a: Int): Int"
    assert blocks["I"].signature == "interface I"


def test_swift_blocks(tmp_path):
    # class/struct/enum all parse as class_declaration in the Swift grammar.
    assert _shape(_blocks(tmp_path, "m.swift", SWIFT)) == [
        ("C", "class", (1, 5)),
        ("m", "function", (2, 4)),
        ("S", "class", (7, 9)),
        ("foo", "function", (11, 13)),
        ("P", "interface", (15, 17)),
        ("f", "method", (16, 16)),
        ("E", "class", (19, 21)),
    ]


def test_signature_is_raw_source_header(tmp_path):
    # The receiver and named returns survive because the signature is a source
    # slice, not a reconstruction from parsed parameter nodes.
    blocks = {b.name: b for b in _blocks(tmp_path, "m.go", GO)}
    assert blocks["Bar"].signature == "func (s *S) Bar(ctx Ctx) (int, error)"
    assert blocks["Foo"].signature == "func Foo(a int, b string) error"


def test_signature_collapses_and_caps(tmp_path):
    blocks = _blocks(tmp_path, "m.py", PY)
    assert all(b.signature and "\n" not in b.signature for b in blocks)
    assert [b.signature for b in blocks] == [
        "def foo(a: int) -> str:",
        "class C(B):",
        "def m(self):",
    ]


def test_unknown_suffix_yields_no_blocks(tmp_path):
    assert _blocks(tmp_path, "m.txt", "def foo(): pass\n") == []


def test_supported_suffixes_covers_all_registered():
    for suffix in (
        ".py", ".js", ".ts", ".tsx", ".go", ".rs", ".java",
        ".cs", ".kt", ".kts", ".swift",
    ):
        assert suffix in SUPPORTED_SUFFIXES
