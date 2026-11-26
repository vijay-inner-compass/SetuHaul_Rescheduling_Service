"""The checks ForgeCI runs: make .venv with this Python, install, then ruff, mypy and pytest. Stops at the first failure.

Run with Python 3.12: ``py -3.12 scripts/ci.py`` on Windows, ``python3.12 scripts/ci.py`` on macOS or Linux.
It is a script, not a shell chain, because ForgeCI uses cmd.exe on Windows and bash elsewhere.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def run(*command: str) -> None:
    print(">", " ".join(command), flush=True)
    code = subprocess.run(command, cwd=ROOT, check=False).returncode
    if code:
        sys.exit(code)


def main() -> None:
    if sys.version_info[:2] != (3, 12):
        sys.exit(f"Python 3.12 is needed; this is {sys.version.split()[0]}")
    run(sys.executable, "-m", "venv", str(VENV))
    python = str(PYTHON)
    run(python, "-m", "pip", "install", "-q", "-e", ".[dev]")
    run(python, "-m", "ruff", "check", ".")
    run(python, "-m", "mypy", "src")
    run(python, "-m", "pytest", "--junitxml=.engineering/results/junit.xml")


if __name__ == "__main__":
    main()
