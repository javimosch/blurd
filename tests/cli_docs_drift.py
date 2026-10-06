#!/usr/bin/env python3
"""Guard: ui/cli-docs.json must be regenerated whenever src/guide.py changes —
the dashboard CLI tab serves the committed JSON."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.exit(subprocess.call(
    [sys.executable, str(ROOT / "spec" / "render_cli_docs.py"), "--check"]))
