"""Output contract: JSON on stdout by default, logs on stderr, always."""

import json
import sys
from datetime import datetime, timezone

from . import OUTPUT_VERSION


class Out:
    def __init__(self, human: bool = False):
        self.human = human

    def log(self, msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    def emit(self, data, human_lines=None) -> None:
        if self.human:
            if human_lines is not None:
                for line in human_lines:
                    print(line)
            else:
                _human(data)
            return
        json.dump({"version": OUTPUT_VERSION, "data": data,
                   "timestamp": datetime.now(timezone.utc)
                   .isoformat(timespec="seconds").replace("+00:00", "Z")},
                  sys.stdout, default=str)
        sys.stdout.write("\n")

    def fail(self, err_dict) -> None:
        json.dump(err_dict, sys.stderr, default=str)
        sys.stderr.write("\n")


def _human(data, indent=0):
    pad = "  " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)) and v:
                print(f"{pad}{k}:")
                _human(v, indent + 1)
            else:
                print(f"{pad}{k}: {v}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                _human(item, indent)
                print()
            else:
                print(f"{pad}- {item}")
    else:
        print(f"{pad}{data}")
