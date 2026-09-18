"""The metadata seam.

blurd stores its metadata in one of two shapes, and nothing outside this module
knows which:

    db_sql.py     normalised tables, SQLite or Postgres  (src/dialect.py)
    db_mongo.py   denormalised documents, MongoDB

Both implement the same ~70 functions with the same names, arguments and return
shapes, so every call site reads `db.query_artifacts(conn, ...)` regardless.
`tests/seam_check.py` keeps SQL confined to `db_sql.py`; that guard is what made
a second backend a day's work rather than an archaeology project.

Dispatch is on the connection object, not on configuration. A process can hold
connections to different backends at once -- the migration tooling does -- and a
global "which backend are we" flag would silently send those queries to the
wrong place. The connection knows what it is; ask it.
"""

from . import db_sql

# Shared, backend-independent helpers: pure functions over values, not rows.
from .db_sql import (COUNT_CAP, INSTANCE_STALE_SECONDS, JOB_SORTS, SORTS,
                     STATS_TTL, decode_cursor, encode_cursor, now)

_MONGO = {"mod": None}


def _mongo():
    """Imported lazily: pymongo is an optional dependency, and a SQLite-only
    deployment must not be made to install it."""
    if _MONGO["mod"] is None:
        from . import db_mongo
        _MONGO["mod"] = db_mongo
    return _MONGO["mod"]


def _is_mongo(conn) -> bool:
    # Duck-typed rather than isinstance, so this stays true without importing
    # pymongo on a deployment that does not use it.
    return getattr(conn, "is_mongo", False) is True


def dialect(cfg=None):
    return db_sql.dialect(cfg)


def connect(db_file=None):
    return dialect().connect()


def init(db_file=None) -> None:
    """Create or bring forward whatever the configured backend stores in."""
    d = dialect()
    if d.name == "mongo":
        conn = d.connect()
        with d.migration_lock(conn):
            _mongo().init(conn)
        return
    return db_sql.init(db_file)


# --- dispatch -----------------------------------------------------------------
#
# Generated rather than hand-written: 60-odd identical three-line wrappers are
# 60-odd chances to route one function to the wrong backend, and the bug would
# surface as "this one filter ignores the tenant" long after the change.

_SHARED = {"now", "encode_cursor", "decode_cursor", "dialect", "connect", "init"}

_DISPATCHED = sorted(
    n for n, v in vars(db_sql).items()
    if callable(v) and not n.startswith("_") and n not in _SHARED
    and getattr(v, "__module__", None) == db_sql.__name__)


def _make(name):
    sql_fn = getattr(db_sql, name)

    def wrapper(*args, **kwargs):
        if args and _is_mongo(args[0]):
            return getattr(_mongo(), name)(*args, **kwargs)
        return sql_fn(*args, **kwargs)

    wrapper.__name__ = name
    wrapper.__qualname__ = name
    wrapper.__doc__ = sql_fn.__doc__
    return wrapper


for _name in _DISPATCHED:
    globals()[_name] = _make(_name)
del _name

__all__ = sorted(_DISPATCHED + list(_SHARED) +
                 ["SORTS", "JOB_SORTS", "COUNT_CAP", "STATS_TTL",
                  "INSTANCE_STALE_SECONDS"])
