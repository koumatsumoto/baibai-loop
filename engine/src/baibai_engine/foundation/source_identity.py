"""Identify a Python source file by what it computes rather than by its bytes.

Build fingerprints fold in the implementation of the modules that decide a row's value,
so that a change nobody declared cannot carry into a build that presents itself as
comparable. Hashing the file's bytes overstates that question: a comment, a docstring or
a reformat cannot move a number, yet it invalidates every cohort built under the previous
byte. Measured on the calibration panel's 80-file closure, 31.4% of the hashed bytes are
comments, docstrings and formatting — and both fingerprint firings on 2026-08-17 were of
exactly that kind, one from a single docstring line and one from a dependency patch that
provably wrote identical Parquet.

The digest is taken over the abstract syntax tree with positions dropped and docstrings
removed, so it moves when the code computes something different and stays put when only
its presentation changes. It is still tied to the interpreter that parses it: a Python
upgrade changes the dump and forces a rebuild. That is the intended behaviour — the
language's own semantics are an input to what a build produced.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

__all__ = ["semantic_source_digest"]

_DOCSTRING_OWNERS = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _without_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_OWNERS):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            # A module or class whose only statement was its docstring still needs a
            # body, and `pass` is the shortest statement that parses back the same way.
            node.body = body[1:] or [ast.Pass()]
    return tree


def semantic_source_digest(path: Path) -> str:
    """Digest what ``path`` computes, ignoring comments, docstrings and formatting."""

    source = path.read_text(encoding="utf-8")
    tree = _without_docstrings(ast.parse(source, filename=str(path)))
    dump = ast.dump(tree, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(dump.encode("utf-8")).hexdigest()
