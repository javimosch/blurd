#!/usr/bin/env python3
"""Guard the metadata seam.

Two invariants, both of which had to hold before a second backend was possible:

  1. `db_sql.py` is the only module that executes SQL. This is not style: it is
     the precondition for swapping SQLite for Postgres, and then for putting
     MongoDB behind the same calls (spec/distributed.md, step 0). The blob
     equivalent was learned the hard way -- paths were built in five modules and
     "pluggable storage" quietly meant "pluggable where someone remembered".

  2. `db_mongo.py` implements exactly the set of functions `db.py` dispatches.
     A backend missing one fails at the call, in production, on whichever
     endpoint happens to need it -- so the parity is checked here instead.

Run: python3 tests/seam_check.py
"""

import pathlib
import re
import sys

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"

# db_sql.py owns every SQL query about blurd's own data. db.py is the facade
# that routes to it or to db_mongo.py, and contains no queries at all.
#
# Two files are allowed near SQL, for precise reasons:
#   scope.py  may BUILD predicate fragments (the predicate is the scope's own
#             logic) but must never execute them.
#   dialect.py may execute ENGINE metadata only -- PRAGMA, sqlite_master,
#             information_schema, VACUUM -- because that is what differs
#             between backends. It must never touch a blurd table; the moment
#             it does, business logic has leaked into the driver layer.
OWNER = "db_sql.py"
FRAGMENT_ONLY = {"scope.py"}
NO_SQL_AT_ALL = {"db.py", "db_mongo.py"}
ENGINE_ONLY = {"dialect.py"}

# blurd's own tables. Seeing one of these in dialect.py is the actual failure.
BLURD_TABLES = re.compile(
    r"\b(images|artifacts|detections|tags|metadata|external_ids|jobs|api_keys|"
    r"audit|thumbs|instances)\b")

EXEC = re.compile(r"\b(?:conn|cur|cursor)\.execute(?:many|script)?\s*\(")
SQL = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|"
                 r"CREATE\s+TABLE|ALTER\s+TABLE|DROP\s+TABLE|VACUUM|PRAGMA)\b")


def main() -> int:
    failures = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == OWNER:
            continue
        text = path.read_text()
        for n, line in enumerate(text.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if path.name in ENGINE_ONLY:
                # Only a reference to a blurd table is a violation here.
                if BLURD_TABLES.search(line) and (EXEC.search(line) or SQL.search(line)):
                    failures.append((path.name, n, "touches a blurd table",
                                     line.strip()[:70]))
                continue
            if EXEC.search(line):
                failures.append((path.name, n, "executes SQL", line.strip()[:70]))
            elif SQL.search(line) and path.name not in FRAGMENT_ONLY:
                failures.append((path.name, n, "contains SQL", line.strip()[:70]))

    if failures:
        print(f"seam broken: {len(failures)} occurrence(s) outside {OWNER}\n")
        for name, n, why, snippet in failures:
            print(f"  {name}:{n}  {why}\n      {snippet}")
        print(f"\nMove it into {OWNER} as a named function. See spec/distributed.md.")
        return 1

    owned = len(EXEC.findall((SRC / OWNER).read_text()))
    print(f"seam intact: all {owned} SQL statements live in {OWNER}")

    return check_parity()


def check_parity() -> int:
    """Every function db.py dispatches must exist in both backends."""
    sys.path.insert(0, str(SRC.parent))
    from src import db, db_mongo, db_sql

    missing = [n for n in db._DISPATCHED if not hasattr(db_mongo, n)]
    stray = [n for n, v in vars(db_mongo).items()
             if callable(v) and not n.startswith("_") and n != "init"
             and getattr(v, "__module__", None) == db_mongo.__name__
             and not hasattr(db_sql, n)]
    if missing:
        print(f"\nbackend parity broken: db_mongo.py is missing "
              f"{len(missing)} function(s)")
        for n in missing:
            print(f"  {n}")
        return 1
    if stray:
        print(f"\nbackend parity broken: db_mongo.py defines {len(stray)} "
              f"function(s) db_sql.py does not")
        for n in stray:
            print(f"  {n}")
        return 1
    print(f"backend parity: db_mongo.py implements all "
          f"{len(db._DISPATCHED)} dispatched functions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
