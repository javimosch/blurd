#!/usr/bin/env python3
"""blurd entry point. Absolute imports only, so `python3 run.py` and the
installed launcher behave identically."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.main import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
