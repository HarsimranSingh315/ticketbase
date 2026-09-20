"""
Guards against the exact bug class that broke this project on a
contributor's Python 3.9: `X | None` (PEP 604 union syntax) used in code
that Python evaluates at runtime, which only works on Python 3.10+.

This runs locally under `pytest` (unlike the CI job's actual Python 3.9
import, which needs a real 3.9 interpreter this sandbox doesn't have) -
so it's a fast, always-available check that doesn't depend on which
Python version happens to be running the test suite.

A file is exempt if it starts with `from __future__ import annotations`,
since that defers all annotation evaluation to strings and makes `X |
None` safe even on 3.9 - `app/supportrag.py` relies on exactly that.
"""
import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _iter_annotation_nodes(tree: ast.AST):
    """
    Yields only the AST nodes that are actually TYPE ANNOTATIONS -
    function parameter annotations, return annotations, and variable
    annotations - not every `X | Y` expression in the file. A genuine
    runtime bitwise-OR (e.g. combining flags) would otherwise show up
    as a false positive, since `ast.BinOp(BitOr)` is how Python parses
    both `int | int` bitwise-or AND `SomeType | None` union syntax when
    there's no `from __future__ import annotations` to keep annotations
    as unevaluated strings.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
            if node.returns is not None:
                yield node.returns
            for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                if arg.annotation is not None:
                    yield arg.annotation
            if node.args.vararg and node.args.vararg.annotation is not None:
                yield node.args.vararg.annotation
            if node.args.kwarg and node.args.kwarg.annotation is not None:
                yield node.args.kwarg.annotation
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


def _uses_unguarded_pep604_union(path: Path) -> list[int]:
    """Returns line numbers using `X | None`-style unions, specifically
    in type-annotation position, without the `from __future__ import
    annotations` guard."""
    source = path.read_text()
    if "from __future__ import annotations" in source:
        return []

    tree = ast.parse(source, filename=str(path))
    bad_lines = []
    for annotation in _iter_annotation_nodes(tree):
        for node in ast.walk(annotation):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
                bad_lines.append(node.lineno)
    return bad_lines


def test_no_unguarded_pep604_unions_in_app_code():
    offenders = {}
    for py_file in APP_DIR.rglob("*.py"):
        bad_lines = _uses_unguarded_pep604_union(py_file)
        if bad_lines:
            offenders[str(py_file)] = bad_lines

    assert not offenders, (
        "Found `X | None`-style unions without `from __future__ import "
        "annotations` - these break on Python 3.9 (e.g. macOS system "
        "Python) at runtime, not just at import/lint time. Either add "
        "`from __future__ import annotations` to the top of the file, "
        "or use `typing.Optional[X]` / `typing.Union[X, Y]` instead.\n"
        f"{offenders}"
    )
