#!/usr/bin/env python3
"""Guard: ui/api-docs.json must be regenerated whenever spec/openapi.yaml
changes — the dashboard API tab serves the committed JSON, so a stale file
documents endpoints that no longer exist (or hides ones that do). Same role
as schema_drift.py, for the docs surface."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.exit(subprocess.call(
    [sys.executable, str(ROOT / "spec" / "render_api_docs.py"), "--check"]))
