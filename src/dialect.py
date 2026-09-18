"""Database dialects.

`db_sql.py` owns every SQL statement (enforced by `tests/seam_check.py`), and those
statements are written to be accepted by both engines: `ON CONFLICT` rather than
`INSERT OR IGNORE`, `RETURNING id` rather than `lastrowid`, an alias on every
subquery, and `jobs.id` rather than `rowid` as the cursor tiebreaker.

What remains genuinely per-engine lives here:

  * the placeholder style -- SQLite takes `?`, psycopg takes `%s`
  * connecting, and how connections are reused
  * running the schema script
  * introspection, for migrations
  * VACUUM, which Postgres refuses inside a transaction

The wrapper is what keeps `db.py` single-source: statements are written in `?`
style and rewritten on the way out. Nothing else in the codebase knows which
engine is underneath.
"""

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

from .errors import Internal, ValidationError

SPEC = Path(__file__).resolve().parent.parent / "spec"

# `?` outside a string literal. blurd's SQL contains no `?` inside literals and
# no literal `%`, both asserted by tests/dialect_check.py -- a naive swap would
# be wrong the moment either appears.
_PLACEHOLDER = re.compile(r"\?")


class _PgCursorWrapper:
    """Gives a psycopg cursor the two habits db.py relies on: `.fetchone()`
    returning a mapping, and `.rowcount` after an UPDATE."""

    def __init__(self, cur):
        self._cur = cur

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    def __iter__(self):
        return iter(self._cur)

    @property
    def rowcount(self):
        return self._cur.rowcount


class _PgConnection:
    """A psycopg connection that accepts db.py's `?`-style SQL.

    Also translates the named-parameter form (`:name`) that a couple of inserts
    use, since psycopg spells it `%(name)s`.
    """

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql: str, params=None):
        sql, params = _adapt(sql, params)
        cur = self._conn.cursor()
        cur.execute(sql, params)
        return _PgCursorWrapper(cur)

    def executemany(self, sql: str, seq_of_params):
        sql, _ = _adapt(sql, None)
        cur = self._conn.cursor()
        cur.executemany(sql, [tuple(p) for p in seq_of_params])
        return _PgCursorWrapper(cur)

    def executescript(self, script: str):
        # psycopg happily runs a multi-statement string; no splitting needed.
        cur = self._conn.cursor()
        cur.execute(script)
        self._conn.commit()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    @property
    def raw(self):
        return self._conn


def _adapt(sql: str, params):
    """Rewrite `?` / `:name` placeholders into psycopg's pyformat."""
    if ":" in sql and re.search(r":\w+", sql):
        sql = re.sub(r":(\w+)", r"%(\1)s", sql)
        return sql, params
    return _PLACEHOLDER.sub("%s", sql), params


import contextlib


class SqliteDialect:
    name = "sqlite"
    schema_file = "schema.sql"

    def __init__(self, cfg):
        self.cfg = cfg
        self._local = threading.local()

    def describe(self) -> dict:
        return {"backend": "sqlite", "path": str(self.cfg.db_file)}

    def connect(self):
        """One connection per thread. WAL gives concurrent readers alongside the
        single writer; the busy timeout absorbs writer contention that a
        threaded HTTP server would otherwise surface as 'database is locked'."""
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        key = str(self.cfg.db_file)
        if key not in conns:
            Path(self.cfg.db_file).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.cfg.db_file, timeout=5.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
            conns[key] = conn
        return conns[key]

    def schema_sql(self) -> str:
        return (SPEC / self.schema_file).read_text()

    def tables(self, conn) -> set:
        return {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def columns(self, conn, table: str) -> set:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

    def vacuum(self, conn) -> None:
        conn.execute("VACUUM")

    def set_foreign_keys(self, conn, on: bool) -> None:
        conn.execute(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}")

    def supports_legacy_migrations(self) -> bool:
        """The 0.2-0.5 migrations only ever ran against SQLite files; a fresh
        Postgres database starts at the current schema and needs none of them."""
        return True

    @property
    def shareable(self) -> bool:
        """A single-writer file. Two processes on one is silent corruption."""
        return False

    @contextlib.contextmanager
    def migration_lock(self, conn):
        """No peers are possible on SQLite -- the instance guard refuses a
        second process -- so there is nothing to serialise against."""
        yield

    def is_unique_violation(self, exc) -> bool:
        return isinstance(exc, sqlite3.IntegrityError) and "UNIQUE" in str(exc).upper()


class PostgresDialect:
    name = "postgres"
    schema_file = "schema.postgres.sql"

    def __init__(self, cfg):
        self.cfg = cfg
        self.dsn = cfg.get("db.dsn") or ""
        if not self.dsn:
            raise ValidationError(
                "The postgres backend needs a DSN",
                suggestions=["BLURD_DB_DSN=postgresql://user:pass@host:5432/blurd",
                             "or: blurd config set db.dsn postgresql://..."])
        try:
            import psycopg  # noqa: F401
        except ImportError:
            raise ValidationError(
                "psycopg is not installed",
                suggestions=["pip install 'psycopg[binary]'",
                             "it is an optional dependency: only the postgres "
                             "backend needs it"])
        self._local = threading.local()

    def describe(self) -> dict:
        # Never echo the DSN: it carries the password.
        import urllib.parse
        p = urllib.parse.urlsplit(self.dsn)
        return {"backend": "postgres", "host": p.hostname, "port": p.port,
                "database": (p.path or "/").lstrip("/")}

    def connect(self):
        """One connection per thread, same shape as SQLite.

        A real pool belongs here before this runs with many replicas -- N
        replicas x M workers against a default `max_connections` of 100 is an
        outage waiting to happen. Noted in spec/distributed.md, step 2.
        """
        import psycopg
        from psycopg.rows import dict_row

        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        if "c" not in conns or conns["c"].raw.closed:
            raw = psycopg.connect(self.dsn, row_factory=dict_row,
                                  connect_timeout=10)
            conns["c"] = _PgConnection(raw)
        return conns["c"]

    def schema_sql(self) -> str:
        return (SPEC / self.schema_file).read_text()

    def tables(self, conn) -> set:
        return {r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema()")}

    def columns(self, conn, table: str) -> set:
        return {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,))}

    def vacuum(self, conn) -> None:
        # Postgres refuses VACUUM inside a transaction block.
        raw = conn.raw
        previous = raw.autocommit
        raw.autocommit = True
        try:
            conn.execute("VACUUM")
        finally:
            raw.autocommit = previous

    def set_foreign_keys(self, conn, on: bool) -> None:
        pass          # no global switch, and the migrations that used it are SQLite-only

    def supports_legacy_migrations(self) -> bool:
        return False

    @property
    def shareable(self) -> bool:
        return True

    @contextlib.contextmanager
    def migration_lock(self, conn):
        """Serialise schema work across replicas.

        Three pods starting together would otherwise run the migration and the
        schema script simultaneously. The lock is session-scoped and released
        explicitly; the key is an arbitrary constant, shared by every blurd
        instance against this database."""
        conn.execute("SELECT pg_advisory_lock(%s)" % _MIGRATION_LOCK_KEY)
        try:
            yield
        finally:
            try:
                conn.execute("SELECT pg_advisory_unlock(%s)" % _MIGRATION_LOCK_KEY)
                conn.commit()
            except Exception:
                pass          # the lock dies with the session anyway

    def is_unique_violation(self, exc) -> bool:
        try:
            import psycopg.errors
        except ImportError:
            return False
        return isinstance(exc, psycopg.errors.UniqueViolation)


class MongoDialect:
    """MongoDB.

    Not a SQL dialect at all, which is the point: it is selected the same way
    and answers the same five questions the rest of the code asks a backend
    (name, connect, shareable, unique-violation, migration lock), while the
    queries themselves live in db_mongo.py rather than db_sql.py.

    There is no schema script and no introspection, so `schema_sql`, `tables`
    and `columns` are absent by design -- anything that reaches for them has
    assumed SQL and should be dispatching instead.
    """

    name = "mongo"

    def __init__(self, cfg):
        self.cfg = cfg
        self.dsn = cfg.get("db.dsn") or ""
        if not self.dsn:
            raise ValidationError(
                "The mongo backend needs a DSN",
                suggestions=["BLURD_DB_DSN=mongodb://user:pass@host:27017/blurd",
                             "or: blurd config set db.dsn mongodb://..."])
        try:
            import pymongo  # noqa: F401
        except ImportError:
            raise ValidationError(
                "pymongo is not installed",
                suggestions=["pip install pymongo",
                             "it is an optional dependency: only the mongo "
                             "backend needs it"])
        self.database = cfg.get("db.database") or "blurd"
        self._local = threading.local()

    def describe(self) -> dict:
        import urllib.parse
        p = urllib.parse.urlsplit(self.dsn)
        return {"backend": "mongo", "host": p.hostname, "port": p.port,
                "database": self.database}

    def connect(self):
        """One client per thread, mirroring the other two backends.

        pymongo's MongoClient is itself thread-safe and pools internally, so a
        shared client would be defensible -- but blurd's `conn` is a
        thread-local everywhere else, and a backend that is the exception is a
        backend whose lifetime bugs are found last.
        """
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        if "c" not in conns:
            import pymongo
            client = pymongo.MongoClient(
                self.dsn, serverSelectionTimeoutMS=10000, tz_aware=False)
            conns["c"] = MongoConnection(client, self.database)
        return conns["c"]

    @property
    def shareable(self) -> bool:
        return True

    @contextlib.contextmanager
    def migration_lock(self, conn):
        """Serialise index creation across replicas.

        Mongo has no advisory locks, so this is a lease row: one replica wins
        the insert on a unique `_id` and the others wait for it to disappear.
        `createIndex` is idempotent, so losing the race is harmless -- the lock
        exists to stop three replicas building the same indexes at once on a
        cold start, not to protect correctness.
        """
        import time
        import pymongo.errors
        locks = conn.db["locks"]
        deadline = time.time() + 60
        held = False
        while time.time() < deadline:
            try:
                locks.insert_one({"_id": "migrate", "at": time.time()})
                held = True
                break
            except pymongo.errors.DuplicateKeyError:
                stale = locks.find_one({"_id": "migrate"})
                if stale and time.time() - stale.get("at", 0) > 120:
                    locks.delete_one({"_id": "migrate"})   # owner died holding it
                    continue
                time.sleep(0.5)
        try:
            yield
        finally:
            if held:
                try:
                    locks.delete_one({"_id": "migrate"})
                except Exception:
                    pass

    def is_unique_violation(self, exc) -> bool:
        try:
            import pymongo.errors
        except ImportError:
            return False
        return isinstance(exc, pymongo.errors.DuplicateKeyError)

    def vacuum(self, conn) -> None:
        # Mongo reclaims space on its own; there is no VACUUM to run and
        # pretending otherwise would report a compaction that did not happen.
        raise ValidationError(
            "VACUUM does not apply to the mongo backend",
            {"backend": "mongo"},
            ["MongoDB reclaims space itself; nothing to run"])


class MongoConnection:
    """What db_mongo.py is handed.

    `commit` and `rollback` are no-ops rather than errors: every call site in
    blurd calls `commit()` after a write, and making the mongo backend the one
    that explodes on it would push backend knowledge back out into the callers
    -- exactly what the seam exists to prevent. blurd writes one document at a
    time and never needs a multi-document transaction, so there is nothing to
    commit. (A standalone mongod could not offer one anyway: transactions need
    a replica set.)
    """

    is_mongo = True

    def __init__(self, client, database: str):
        self.client = client
        self.db = client[database]

    def commit(self):
        pass

    def rollback(self):
        pass

    def execute(self, *a, **k):
        raise Internal("SQL was executed against the mongo backend -- a call "
                       "site is bypassing db.py's dispatch")


# Arbitrary, but must be stable: every blurd instance against one database has
# to pick the same number for the lock to mean anything.
_MIGRATION_LOCK_KEY = 8071975


_CACHE = {}


def build(cfg):
    """Chosen once per config. `BLURD_DB_BACKEND` is applied by Config, so a
    container can select it without writing a config file."""
    backend = (cfg.get("db.backend", "sqlite") or "sqlite").lower()
    key = (backend, str(cfg.home), cfg.get("db.dsn") or "")
    if key in _CACHE:
        return _CACHE[key]
    if backend == "sqlite":
        d = SqliteDialect(cfg)
    elif backend in ("postgres", "postgresql", "pg"):
        d = PostgresDialect(cfg)
    elif backend in ("mongo", "mongodb"):
        d = MongoDialect(cfg)
    else:
        raise ValidationError(f"Unknown database backend '{backend}'",
                              {"known": ["sqlite", "postgres", "mongo"]},
                              ["blurd config set db.backend sqlite|postgres|mongo"])
    _CACHE[key] = d
    return d
