#!/usr/bin/env python3
"""Render the embedded CLI guide -> ui/cli-docs.json for the dashboard's CLI tab.

Same deal as render_api_docs.py: committed JSON, no runtime work, and a
--check mode (tests/cli_docs_drift.py) that fails when src/guide.py moves on
without a re-render. The source is src/guide.py itself — the same document
`blurd guide` and `--help-json` emit — so the tab can never disagree with the
binary it documents.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "ui" / "cli-docs.json"

from src import guide  # noqa: E402 -- import after sys.path fix


def render():
    return {
        "generated_from": "src/guide.py",
        "guide": guide.as_guide(),
        "catalog": guide.as_json(),
        "text": guide.text(),
    }


def main():
    doc = render()
    if "--check" in sys.argv:
        current = json.loads(OUT.read_text()) if OUT.exists() else None
        if current != doc:
            print("ui/cli-docs.json is stale — run spec/render_cli_docs.py")
            sys.exit(1)
        print(f"cli docs in sync: {len(doc['catalog']['commands'])} commands")
        return
    OUT.write_text(json.dumps(doc, indent=2) + "\n")
    print(f"wrote {OUT}: {len(doc['catalog']['commands'])} commands")


if __name__ == "__main__":
    main()
