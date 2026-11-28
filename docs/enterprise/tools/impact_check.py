"""Incremental documentation impact analyzer for docs/enterprise/.

Reads docs/enterprise/knowledge-model/knowledge-model.json and compares changed
repository paths from `git diff --name-only <base_ref>..HEAD` (plus working tree
if --working is passed) to report impacted enterprise documents (DOC-*).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
KM_PATH = ROOT / "docs" / "enterprise" / "knowledge-model" / "knowledge-model.json"


def _matches(changed: str, pattern: str) -> bool:
    norm_changed = changed.replace("\\", "/")
    norm_pattern = pattern.replace("\\", "/")
    if norm_pattern.endswith("/"):
        return norm_changed.startswith(norm_pattern)
    return fnmatch.fnmatch(norm_changed, norm_pattern) or norm_changed == norm_pattern


def _changed_files(base_ref: str, include_working: bool) -> list[str]:
    cmd = ["git", "diff", "--name-only", base_ref] if include_working else [
        "git",
        "diff",
        "--name-only",
        f"{base_ref}..HEAD",
    ]
    out = subprocess.check_output(cmd, cwd=ROOT, text=True)
    return [line.strip() for line in out.splitlines() if line.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect impacted enterprise docs from git diff.")
    parser.add_argument("base_ref", nargs="?", default="HEAD~1", help="Base git ref (default: HEAD~1)")
    parser.add_argument("--working", action="store_true", help="Compare base_ref against working tree")
    args = parser.parse_args(argv)

    km = json.loads(KM_PATH.read_text(encoding="utf-8"))
    changed = _changed_files(args.base_ref, args.working)
    if not changed:
        print("No repository changes detected.")
        return 0

    impacted_components: dict[str, list[str]] = {}
    impacted_docs: set[str] = set()

    for comp in km.get("components", []):
        matched = [f for f in changed if any(_matches(f, p) for p in comp.get("paths", []))]
        if matched:
            impacted_components[f"{comp['id']} ({comp['name']})"] = matched
            impacted_docs.update(comp.get("affected_docs", []))

    print(f"Baseline commit in knowledge model: {km.get('commit_ref')}")
    print(f"Changed files ({len(changed)}): {', '.join(changed[:10])}{'...' if len(changed) > 10 else ''}")
    if not impacted_components:
        print("No mapped application components impacted.")
        return 0

    print("\nImpacted Components:")
    for comp_label, files in impacted_components.items():
        print(f"  - {comp_label}: {', '.join(files)}")

    impacted_docs.update({"DOC-21", "DOC-22", "DOC-24"})
    print("\nEnterprise Documents Requiring Incremental Review:")
    for doc_id in sorted(impacted_docs):
        print(f"  - {doc_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
