"""Prove that a documentation pass changed no behaviour: every .py file must parse to the same syntax tree as the
committed version once docstrings are removed (comments never reach the tree, so a comment-only edit always passes).

Usage: python scripts/check_comment_only.py [BASE_REF]   (default HEAD)
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    # A docstring is a bare string expression that is the first statement of a module, class or function body.
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:] or [ast.Pass()]  # keep the body non-empty so the tree stays valid
    return tree


def _dump(source: str) -> str:
    return ast.dump(_strip_docstrings(ast.parse(source)))


def main(base: str = "HEAD") -> int:
    changed = subprocess.run(["git", "diff", "--name-only", base, "--", "src", "scripts", "tests"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.split()
    bad = 0
    for rel in (c for c in changed if c.endswith(".py")):
        # Read the committed bytes and decode them as UTF-8 ourselves: the Windows default code page cannot read Devanagari text.
        old = subprocess.run(["git", "show", f"{base}:{rel}"], cwd=ROOT, capture_output=True)
        if old.returncode != 0:  # a new file has nothing to compare with
            continue
        if _dump(old.stdout.decode("utf-8")) != _dump((ROOT / rel).read_text(encoding="utf-8")):
            print("CODE CHANGED (not comment-only):", rel)
            bad += 1
    print(f"{len(changed)} changed file(s) checked, {bad} with code changes")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
