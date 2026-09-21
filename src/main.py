"""CLI entry point. Argument parsing and dispatch only -- every command routes
through client.build(), so local and --remote behave identically."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__, client, guide, models
from .config import Config
from .daemon import Daemon
from .errors import (BlurdError, EXIT_GENERIC_FAILURE, EXIT_SUCCESS,
                     Internal, InvalidArgument, ValidationError)
from .output import Out


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 with usage text on any error; the output contract wants
    a typed error body and a semantic exit code instead."""
    def error(self, message):
        raise InvalidArgument(message,
                              suggestions=["Run: blurd --help",
                                           "Run: blurd guide"])


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="blurd", add_help=True,
                                description="Image redaction: faces + licence plates")
    p.add_argument("--human", action="store_true", help="Human-readable output")
    p.add_argument("--json", action="store_true", help="JSON output (default)")
    p.add_argument("--remote", help="Base URL of a blurd daemon")
    p.add_argument("--api-key", help="API key for --remote (or BLURD_API_KEY)")
    p.add_argument("--home", help="blurd home directory (or BLURD_HOME)")
    p.add_argument("--help-json", action="store_true", help="Machine-readable help")
    sub = p.add_subparsers(dest="cmd")

    b = sub.add_parser("blur", help="Redact one image")
    b.add_argument("source", nargs="?", help="File path, or '-' for stdin")
    b.add_argument("--url", help="Fetch the image from this URL")
    b.add_argument("--code", help="Your own unique id for this image "
                                  "(often the filename); indexed for fast lookup")
    b.add_argument("--async", dest="async_", action="store_true",
                   help="Return the job immediately instead of waiting")
    b.add_argument("--wait", type=float, default=120.0,
                   help="Seconds to wait for the job (default 120)")
    b.add_argument("--on-conflict", choices=["reuse", "replace", "reject"],
                   default="reuse",
                   help="What to do when --code already maps to other bytes")
    b.add_argument("--tag", action="append", default=[])
    b.add_argument("--meta", action="append", default=[], metavar="K=V")
    b.add_argument("--mode", choices=["pixelate", "blur", "solid"])
    b.add_argument("--face-score", type=float)
    b.add_argument("--plate-score", type=float)
    b.add_argument("--ttl", type=int, metavar="SECONDS",
                   help="Prune the redacted blob after N seconds (min 60)")
    b.add_argument("--force", action="store_true")
    b.add_argument("--out", help="Also write the redacted image here")

    l = sub.add_parser("list", help="Filter stored artifacts")
    l.add_argument("--tag", action="append", default=[])
    l.add_argument("--meta", action="append", default=[], metavar="K=V")
    l.add_argument("--sha", help="sha256 prefix")
    l.add_argument("--code", help="external id (exact, or prefix with a trailing *)")
    l.add_argument("--needs-review", action="store_true")
    l.add_argument("--since"); l.add_argument("--until")
    l.add_argument("--limit", type=int, default=50)
    l.add_argument("--offset", type=int, default=0)

    g = sub.add_parser("get")
    g.add_argument("sha", nargs="?"); g.add_argument("--code"); g.add_argument("--profile")
    d = sub.add_parser("download")
    d.add_argument("sha", nargs="?"); d.add_argument("--code")
    d.add_argument("--out", required=True); d.add_argument("--profile")

    jb = sub.add_parser("jobs", help="Inspect async jobs")
    js = jb.add_subparsers(dest="jobs_cmd")
    jl = js.add_parser("list")
    jl.add_argument("--status", choices=["queued", "running", "done", "failed"])
    jl.add_argument("--code"); jl.add_argument("--limit", type=int, default=25)
    jg = js.add_parser("get"); jg.add_argument("id")
    jg.add_argument("--wait", type=float, default=0.0)
    rm = sub.add_parser("delete"); rm.add_argument("sha")
    sub.add_parser("stats")

    s = sub.add_parser("serve", help="Run the API + dashboard")
    s.add_argument("--host"); s.add_argument("--port", type=int)
    s.add_argument("--daemon", action="store_true")
    s.add_argument("--foreground", action="store_true")
    sub.add_parser("stop"); sub.add_parser("status")
    sub.add_parser("help-json", help="Machine-readable command catalog")

    dm = sub.add_parser("daemon", help="Daemon lifecycle (cli-daemon-spec)")
    dms = dm.add_subparsers(dest="daemon_cmd")
    dstart = dms.add_parser("start")
    dstart.add_argument("--host"); dstart.add_argument("--port", type=int)
    dstop = dms.add_parser("stop")
    dstop.add_argument("--host"); dstop.add_argument("--port", type=int)
    dms.add_parser("status")

    k = sub.add_parser("keys"); ks = k.add_subparsers(dest="keys_cmd")
    ka = ks.add_parser("add"); ka.add_argument("name")
    ka.add_argument("--scope-tag", action="append", default=[], metavar="TAG",
                    help="Restrict the key to images carrying this tag (repeatable, ANDed)")
    ka.add_argument("--scope-meta", action="append", default=[], metavar="K=V",
                    help="Restrict the key to images carrying this metadata (repeatable, ANDed)")
    ka.add_argument("--key", metavar="SECRET",
                    help="Register an existing key instead of minting a new one")
    ks.add_parser("list")
    kr = ks.add_parser("revoke"); kr.add_argument("id")
    ke = ks.add_parser("export",
                       help="Export key records (hashes + scopes, never plaintext)")
    ke.add_argument("--out", metavar="FILE",
                    help="Write the export here instead of stdout-only")
    ki = ks.add_parser("import",
                       help="Import key records produced by `keys export`")
    ki.add_argument("file", help="Export file ('-' reads stdin)")

    dp = sub.add_parser("dashboard-password"); dp.add_argument("password")

    dk = sub.add_parser("dashboard-keys",
                        help="Allow or forbid minting API keys from the dashboard")
    dks = dk.add_subparsers(dest="dashboard_keys_cmd")
    dke = dks.add_parser("enable")
    dke.add_argument("--secret", required=True,
                     help="Second secret, required at mint time. Not the dashboard password.")
    dks.add_parser("disable")
    dks.add_parser("status")

    fb = sub.add_parser("feedback",
                        help="Send feedback to this deployment and the shared relay")
    fb.add_argument("message")
    fb.add_argument("-kind", "--kind", default="note",
                    help="bug | idea | praise | note")
    fb.add_argument("-context", "--context", default="",
                    help="What you were doing when this came up")

    au = sub.add_parser("audit", help="Privileged mutations, most recent first")
    au.add_argument("--limit", type=int, default=50)

    m = sub.add_parser("models"); ms = m.add_subparsers(dest="models_cmd")
    ms.add_parser("list")
    mp = ms.add_parser("pull"); mp.add_argument("name", nargs="?")
    mp.add_argument("--all", action="store_true"); mp.add_argument("--force", action="store_true")

    c = sub.add_parser("config"); cs = c.add_subparsers(dest="config_cmd")
    cg = cs.add_parser("get"); cg.add_argument("key", nargs="?")
    cst = cs.add_parser("set"); cst.add_argument("key"); cst.add_argument("value")

    st = sub.add_parser("storage", help="Inspect or verify the blob backend")
    sts = st.add_subparsers(dest="storage_cmd")
    sts.add_parser("show"); sts.add_parser("check")

    mb = sub.add_parser("migrate-blobs",
                        help="Copy blobs between backends (resumable, batched)")
    mb.add_argument("--to", choices=["s3", "local"], required=True)
    mb.add_argument("--batch", type=int, default=200)
    mb.add_argument("--max", type=int, default=0, help="stop after N (0 = all)")
    mb.add_argument("--delete-source", action="store_true",
                    help="remove each blob from the old backend once verified")

    mt = sub.add_parser("migrate-thumbs",
                        help="Move legacy inline thumbnails out of the artifacts table")
    mt.add_argument("--batch", type=int, default=500)
    mt.add_argument("--max", type=int, default=0, help="stop after N (0 = all)")
    sub.add_parser("vacuum", help="Reclaim free pages (blocking; stop the daemon first)")
    sub.add_parser("doctor"); sub.add_parser("guide"); sub.add_parser("version")
    return p


def _clone_cfg(cfg, backend):
    """A view of the config with a different storage backend, so a migration can
    hold both ends open at once."""
    import copy
    clone = type(cfg)(cfg.home)
    clone._data = copy.deepcopy(cfg.data)
    clone._data.setdefault("storage", {})["backend"] = backend
    return clone


def _kv(pairs):
    out = {}
    for item in pairs or []:
        if "=" not in item:
            raise InvalidArgument(f"--meta expects K=V, got '{item}'")
        k, _, v = item.partition("=")
        out[k.strip()] = v
    return out


def _overrides(args) -> dict:
    o = {}
    if getattr(args, "mode", None):
        o.setdefault("redact", {})["mode"] = args.mode
    if getattr(args, "face_score", None) is not None:
        o.setdefault("detect", {}).setdefault("face", {})["min_score"] = args.face_score
    if getattr(args, "plate_score", None) is not None:
        o.setdefault("detect", {}).setdefault("plate", {})["min_score"] = args.plate_score
    if getattr(args, "ttl", None) is not None:
        o.setdefault("storage", {})["ttl"] = args.ttl
    return o


def run(args, out: Out) -> int:
    cfg = Config(Path(args.home).expanduser() if args.home else None)
    from . import db as _bind_db
    _bind_db.dialect(cfg)             # bind the backend before any query runs
    import os
    api_key = args.api_key or os.environ.get("BLURD_API_KEY")
    cmd = args.cmd

    # Commands that are inherently local, regardless of --remote.
    if cmd == "version":
        out.emit({"name": "blurd", "version": __version__,
                  "python": sys.version.split()[0], "home": str(cfg.home)})
        return EXIT_SUCCESS
    if cmd == "guide":
        if args.human:
            print(guide.text())
        else:
            json.dump(guide.as_guide(), sys.stdout)
            sys.stdout.write("\n")
        return EXIT_SUCCESS
    if cmd == "doctor":
        out.emit(doctor(cfg))
        return EXIT_SUCCESS
    if cmd == "models":
        cfg.ensure_dirs()
        if args.models_cmd == "pull":
            names = list(models.REGISTRY) if args.all else ([args.name] if args.name else [])
            if not names:
                raise InvalidArgument("Specify a model name or --all",
                                      {"known": sorted(models.REGISTRY)})
            results = []
            for n in names:
                out.log(f"pulling {n} ...")
                results.append(models.pull(cfg.models_dir, n, force=args.force))
            out.emit(results)
        else:
            out.emit(models.listing(cfg.models_dir))
        return EXIT_SUCCESS
    if cmd == "config":
        if args.config_cmd == "set":
            _warn_if_daemon_running(cfg, out)
            try:
                value = json.loads(args.value)
            except ValueError:
                value = args.value
            cfg.set(args.key, value)
            out.emit({"key": args.key, "value": value, "file": str(cfg.config_file)})
        else:
            out.emit(cfg.get(args.key) if args.key else cfg.data)
        return EXIT_SUCCESS
    if cmd == "dashboard-keys":
        if args.dashboard_keys_cmd == "enable":
            if args.secret == cfg.get("dashboard_password"):
                raise InvalidArgument(
                    "The admin secret must differ from the dashboard password",
                    suggestions=["The point of the second secret is that one "
                                 "compromised browser login is not enough to "
                                 "mint a key that outlives it"])
            # Length is a strength question, and strength is the operator's
            # call -- warn and proceed. Equality with the dashboard password is
            # not: such a secret adds exactly nothing, so that stays an error.
            weak = len(args.secret) < 12
            if weak:
                out.log(f"warning: the admin secret is {len(args.secret)} characters. "
                        "Fine for a local POC; use 12+ anywhere it is reachable.")
            cfg.set("dashboard_allow_key_creation", True)
            cfg.set("dashboard_key_secret", args.secret)
            out.emit({"dashboard_key_creation": "enabled",
                      "requires_header": "X-Blurd-Admin-Secret",
                      "weak_secret": weak,
                      "note": "Revocation was already available and stays available."})
        elif args.dashboard_keys_cmd == "disable":
            cfg.set("dashboard_allow_key_creation", False)
            cfg.set("dashboard_key_secret", None)
            out.emit({"dashboard_key_creation": "disabled"})
        else:
            out.emit({"dashboard_key_creation":
                      "enabled" if cfg.get("dashboard_allow_key_creation") else "disabled",
                      "secret_set": bool(cfg.get("dashboard_key_secret")),
                      "revocation": "always available in the dashboard"})
        return EXIT_SUCCESS

    if cmd == "storage":
        from . import store as _store
        blobs = _store.build(cfg)
        if args.storage_cmd == "check":
            out.emit(blobs.check(), human_lines=[
                f"backend  {blobs.kind}",
                *[f"{k:<8} {v}" for k, v in blobs.describe().items() if k != "backend"],
                "status   reachable, readable and writable",
            ])
        else:
            out.emit(blobs.describe())
        return EXIT_SUCCESS

    if cmd == "migrate-blobs":
        from . import db as _db, store as _store
        import time as _t
        _db.init(cfg.db_file)
        conn = _db.connect(cfg.db_file)
        src_cfg = _clone_cfg(cfg, "local" if args.to == "s3" else "s3")
        source = _store.build(src_cfg)
        dest = _store.build(_clone_cfg(cfg, args.to))
        if source.kind == dest.kind:
            raise InvalidArgument(
                f"Source and destination are both '{dest.kind}'",
                {"configured_backend": cfg.get("storage.backend")},
                ["Set storage.backend to the DESTINATION, then migrate --to it"])
        dest.check()
        paths = _db.all_blob_paths(conn)
        out.log(f"{len(paths)} blob(s) to consider, {source.kind} -> {dest.kind}")
        moved = skipped = missing = 0
        t0 = _t.perf_counter()
        for rel in paths:
            if dest.exists(rel):          # resumable: already-copied blobs are free
                skipped += 1
                continue
            try:
                data = source.get(rel)
            except BlurdError:
                missing += 1
                continue
            dest.put(rel, data)
            if args.delete_source:
                source.delete(rel)
            moved += 1
            if moved % args.batch == 0:
                out.log(f"  {moved} moved, {skipped} already present, {missing} missing")
            if args.max and moved >= args.max:
                break
        out.emit({"moved": moved, "already_present": skipped, "missing": missing,
                  "from": source.kind, "to": dest.kind,
                  "seconds": round(_t.perf_counter() - t0, 1),
                  "next": f"blurd config set storage.backend {dest.kind}"
                          if cfg.get("storage.backend") != dest.kind else None})
        return EXIT_SUCCESS

    if cmd == "migrate-thumbs":
        from . import db as _db
        import time as _t
        conn = _db.connect(cfg.db_file)
        _db.init(cfg.db_file)
        backlog = _db.legacy_thumb_backlog(conn)
        if not backlog:
            out.emit({"moved": 0, "remaining": 0, "status": "nothing to migrate"})
            return EXIT_SUCCESS
        out.log(f"moving {backlog} legacy thumbnails in batches of {args.batch}")
        moved, t0 = 0, _t.perf_counter()
        while True:
            n = _db.migrate_thumbs_batch(conn, args.batch)
            if not n:
                break
            moved += n
            out.log(f"  {moved}/{backlog}")
            if args.max and moved >= args.max:
                break
        remaining = _db.legacy_thumb_backlog(conn)
        out.emit({"moved": moved, "remaining": remaining,
                  "seconds": round(_t.perf_counter() - t0, 1),
                  "next": "blurd vacuum" if not remaining else
                          "run again to continue"})
        return EXIT_SUCCESS

    if cmd == "vacuum":
        import time as _t

        from . import db as _db
        conn = _db.connect(cfg.db_file)
        # Only SQLite has a file to measure. Reading its size on postgres or
        # mongo raised FileNotFoundError before the backend got the chance to
        # say "this does not apply to me", which reported the wrong problem.
        db_path = Path(cfg.db_file)
        backend = _db.dialect().name
        measurable = backend == "sqlite" and db_path.exists()
        before = db_path.stat().st_size if measurable else None
        if backend == "sqlite":
            # Only announced when there is something to announce: a backend
            # that is about to refuse should not first claim to be working.
            out.log("vacuuming; this blocks and can take minutes "
                    "on a large database")
        t0 = _t.perf_counter()
        _db.vacuum(conn)
        after = db_path.stat().st_size if measurable else None
        out.emit({"bytes_before": before, "bytes_after": after,
                  "reclaimed": (before - after) if measurable else None,
                  "seconds": round(_t.perf_counter() - t0, 1)})
        return EXIT_SUCCESS

    if cmd == "audit":
        from . import db as _db
        conn = _db.connect(cfg.db_file)
        rows = _db.audit_list(conn, args.limit)
        out.emit(rows, human_lines=[
            f"  {r['at']}  {r['actor']:<10} {r['action']:<14} "
            f"{(r['target'] or '-'):<20} {r['source_ip'] or ''}" for r in rows
        ] or ["(no privileged mutations recorded)"])
        return EXIT_SUCCESS

    if cmd == "dashboard-password":
        _warn_if_daemon_running(cfg, out)
        cfg.set("dashboard_password", args.password)
        out.emit({"dashboard_user": cfg.get("dashboard_user"),
                  "password_set": True, "file": str(cfg.config_file)})
        return EXIT_SUCCESS
    if cmd in ("serve", "stop", "status"):
        return daemon_cmd(cmd, args, cfg, out)

    if cmd == "daemon":
        dm = Daemon(cfg)
        sub_cmd = args.daemon_cmd
        if sub_cmd == "status":
            out.emit(dm.status()); return EXIT_SUCCESS
        if sub_cmd == "stop":
            out.emit(dm.stop()); return EXIT_SUCCESS
        if sub_cmd == "start":
            host = args.host or cfg.get("host", "127.0.0.1")
            port = args.port or int(cfg.get("port", 8770))
            out.emit(dm.start(host, port)); return EXIT_SUCCESS
        raise InvalidArgument("daemon needs start|stop|status")

    if cmd == "feedback":
        return _feedback(args, api_key, cfg, out)

    cl = client.build(cfg, args.remote, api_key)

    if cmd == "blur":
        if args.async_ and not args.remote:
            # A local run owns its queue and exits with it, so the job would
            # never be picked up. Say so instead of handing back a job id that
            # will sit at "queued" until a daemon garbage-collects it.
            raise InvalidArgument(
                "--async needs a daemon to run the job",
                {"mode": "local"},
                ["Start one: blurd serve --daemon",
                 "Then: blurd --remote http://127.0.0.1:8770 blur ... --async"])
        data = None
        path = None
        if args.source == "-":
            data = sys.stdin.buffer.read()
            if not data:
                raise ValidationError("No image bytes on stdin")
        elif args.source:
            path = args.source
        elif not args.url:
            raise InvalidArgument("Provide a file path, '-' for stdin, or --url")
        job = cl.submit(url=args.url, path=path, data=data, code=args.code,
                        tags=args.tag, metadata=_kv(args.meta),
                        overrides=_overrides(args), force=args.force,
                        on_conflict=args.on_conflict,
                        wait=0.0 if args.async_ else args.wait)
        if args.out and job.get("result"):
            blob = cl.blob(job["source_sha"], job["profile_hash"])
            Path(args.out).write_bytes(blob)
            job = dict(job, written_to=str(Path(args.out).resolve()))
        out.emit(job, human_lines=_job_human(job))
        # A failed job is a failed command: agents must not have to parse the
        # payload to notice that nothing was produced.
        if job["status"] == "failed":
            return int((job.get("error") or {}).get("code", 1))
        return EXIT_SUCCESS

    if cmd == "jobs":
        if args.jobs_cmd == "get":
            job = cl.job(args.id, wait=args.wait)
            out.emit(job, human_lines=_job_human(job))
            return EXIT_SUCCESS
        res = cl.jobs(status=args.status, code=args.code, limit=args.limit)
        out.emit(res, human_lines=_jobs_human(res))
        return EXIT_SUCCESS

    if cmd == "list":
        res = cl.list(tag=args.tag, meta=_kv(args.meta), sha=args.sha, code=args.code,
                      needs_review=True if args.needs_review else None,
                      since=args.since, until=args.until,
                      limit=args.limit, offset=args.offset)
        out.emit(res, human_lines=_list_human(res))
        return EXIT_SUCCESS

    if cmd == "get":
        if not args.sha and not args.code:
            raise InvalidArgument("Provide a sha or --code")
        out.emit(cl.by_code(args.code, args.profile) if args.code
                 else cl.get(args.sha, args.profile))
        return EXIT_SUCCESS

    if cmd == "download":
        if not args.sha and not args.code:
            raise InvalidArgument("Provide a sha or --code")
        if args.code:
            rec = cl.by_code(args.code, args.profile)
            sha = rec["source_sha"]
            blob = cl.blob(sha, rec["profile_hash"])
        else:
            sha = args.sha
            blob = cl.blob(sha, args.profile)
        Path(args.out).write_bytes(blob)
        out.emit({"source_sha": sha, "external_id": args.code,
                  "written_to": str(Path(args.out).resolve()), "bytes": len(blob)})
        return EXIT_SUCCESS

    if cmd == "delete":
        out.emit(cl.delete(args.sha))
        return EXIT_SUCCESS

    if cmd == "stats":
        out.emit(cl.stats())
        return EXIT_SUCCESS

    if cmd == "keys":
        if args.keys_cmd == "add":
            from .scope import from_cli
            sc = from_cli(args.scope_tag, args.scope_meta)
            res = cl.keys_add(args.name, sc, key=args.key)
            out.emit(res, human_lines=[
                f"id       {res['id']}",
                f"name     {res['name']}",
                f"scope    {res.get('scope_description', 'unrestricted')}",
                f"tenant   {res.get('tenant', 'global')}",
                f"key      {res['key']}",
                "",
                "Store the key now; only its hash is kept.",
            ] + ([
                "",
                "This key is scoped: its submissions are stamped with the scope",
                "automatically, its unique codes live in their own namespace, and",
                "it can neither see nor label another tenant's images.",
            ] if sc else []))
        elif args.keys_cmd == "revoke":
            out.emit(cl.keys_revoke(args.id))
        elif args.keys_cmd == "export":
            payload = cl.keys_export()
            if args.out:
                Path(args.out).write_text(json.dumps(payload, indent=2) + "\n")
                out.emit({"written_to": str(Path(args.out).resolve()),
                          "keys": len(payload["keys"])},
                         human_lines=[
                             f"{len(payload['keys'])} key(s) -> {args.out}",
                             "Hashes and scopes only; no plaintext is stored "
                             "or exported."])
            else:
                out.emit(payload)
        elif args.keys_cmd == "import":
            raw = sys.stdin.read() if args.file == "-" \
                else Path(args.file).read_text()
            try:
                payload = json.loads(raw)
            except ValueError:
                raise ValidationError(f"{args.file}: not valid JSON")
            if not isinstance(payload, dict):
                raise ValidationError(f"{args.file}: expected a JSON object")
            if isinstance(payload.get("data"), dict):  # a full CLI envelope
                payload = payload["data"]
            res = cl.keys_import(payload)
            out.emit(res, human_lines=[
                f"imported {res['count']} key(s), "
                f"skipped {len(res['skipped'])}, "
                f"remapped ids {len(res['remapped_ids'])}"])
        else:
            keys = cl.keys_list()
            out.emit(keys, human_lines=[
                f"  {k['id']}  {k['name']:<20} {k['prefix']}…  "
                f"{'revoked' if k['revoked'] else 'active ':<8} "
                f"{k.get('scope_description', 'unrestricted')}" for k in keys
            ] or ["(no keys)"])
        return EXIT_SUCCESS

    raise InvalidArgument(f"Unknown command '{cmd}'",
                          suggestions=["Run: blurd guide"])


def daemon_cmd(cmd, args, cfg, out) -> int:
    dm = Daemon(cfg)
    if cmd == "stop":
        out.emit(dm.stop()); return EXIT_SUCCESS
    if cmd == "status":
        out.emit(dm.status()); return EXIT_SUCCESS

    host = args.host or cfg.get("host", "127.0.0.1")
    port = args.port or int(cfg.get("port", 8770))
    if args.daemon:
        out.emit(dm.start(host, port)); return EXIT_SUCCESS

    # Foreground.
    from . import server
    cfg.ensure_dirs()
    _startup_checks(cfg, out)
    srv = server.serve(cfg, host, port, log=out.log)
    info = {"status": "running", "mode": "foreground", "host": host, "port": port,
            "url": f"http://{host}:{port}", "dashboard": f"http://{host}:{port}/",
            "home": str(cfg.home),
            "dashboard_enabled": bool(cfg.get("dashboard_password"))}
    out.emit(info)
    out.log(f"blurd listening on http://{host}:{port} (ctrl-c to stop)")
    drain = float(cfg.get("drain_seconds", 20) or 0)
    try:
        import signal, threading
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        stop.wait()
    finally:
        # Health starts failing immediately so the load balancer drops us,
        # then in-flight work gets `drain` seconds to finish.
        out.log(f"draining for up to {drain:.0f}s before exit")
        srv.shutdown(drain)
        out.log("blurd stopped")
    return EXIT_SUCCESS


def _feedback(args, api_key, cfg, out) -> int:
    """cli-feedback-spec: one submission, two best-effort writes -- the app's
    own store and the central relay, under the same id so retries are
    idempotent. NEVER fails the caller."""
    import os
    import secrets as _secrets
    body = {"id": _secrets.token_hex(16), "app": "blurd",
            "version": __version__, "kind": args.kind,
            "message": args.message, "context": args.context,
            "reporter": os.environ.get("USER") or "agent"}
    stored, relayed = 0, 0
    app_url = (args.remote or os.environ.get("BLURD_URL")
               or os.environ.get("BLURD_PUBLIC_URL"))
    try:
        if app_url:
            client.RemoteClient(app_url, api_key or "").feedback_submit(body)
        else:
            client.LocalClient(cfg).feedback_submit(body)
        stored = 1
    except Exception as exc:
        out.log(f"feedback: app write failed: {exc}")
    relay = os.environ.get("FEEDBACK_RELAY", "https://feedback.intrane.fr")
    if relay and relay != "off":
        try:
            client.RemoteClient(relay, "").feedback_submit(body)
            relayed = 1
        except Exception as exc:
            out.log(f"feedback: relay write failed: {exc}")
    out.emit({"id": body["id"], "stored": stored, "relayed": relayed})
    return EXIT_SUCCESS


def _warn_if_daemon_running(cfg: Config, out: Out) -> None:
    """A running daemon read its config at startup and will not see this change.
    Silence here looks like the setting did not take."""
    try:
        if Daemon(cfg).is_running():
            out.log("note: a daemon is running and reads its config at startup — "
                    "restart it (blurd stop && blurd serve --daemon) for this to "
                    "take effect, or set the matching BLURD_* variable instead")
    except Exception:
        pass


def _startup_checks(cfg: Config, out: Out) -> None:
    """Fail loudly at boot instead of quietly per-job.

    A container with no models starts fine and then fails every single image,
    which reads like a model bug rather than a deployment one. Same for an
    unreachable bucket.
    """
    import os
    from . import models as _models, store as _store

    missing = [m["name"] for m in _models.listing(cfg.models_dir) if not m["present"]]
    if missing and os.environ.get("BLURD_PULL_MODELS", "").lower() in ("1", "true", "yes"):
        out.log(f"BLURD_PULL_MODELS set; downloading {len(missing)} model(s)")
        for name in missing:
            _models.pull(cfg.models_dir, name)
        missing = [m["name"] for m in _models.listing(cfg.models_dir) if not m["present"]]
    if missing:
        out.log(f"WARNING: {len(missing)} detector model(s) missing ({', '.join(missing)}). "
                "Every submission will fail until they are present -- run "
                "`blurd models pull --all`, set BLURD_PULL_MODELS=1, or use an "
                "init container.")

    from . import resources as _res
    b = _res.worker_budget()
    workers = _res.effective_workers(cfg)
    peak = _res.estimate_peak_mb(workers)
    mem = b["memory_mb"]
    out.log(f"resources: {b['cpus']:.1f} cpu, "
            + (f"{mem} MB available" if mem else "memory unknown")
            + f" -> {workers} worker(s), peak ~{peak} MB")
    if mem is not None and peak > mem * _res.HEADROOM:
        # Explicitly configured too high: the operator's call, but do not let
        # them find out from the OOM killer halfway through a job.
        out.log(f"WARNING: {workers} workers need ~{peak} MB but only {mem} MB "
                f"is available. Expect the process to be killed under load. "
                f"Set BLURD_WORKERS=auto (or a smaller number).")

    try:
        info = _store.build(cfg).check()
        out.log(f"storage: {info['backend']} ok"
                + (f" ({info.get('bucket')} @ {info.get('endpoint')})"
                   if info.get("backend") == "s3" else ""))
    except BlurdError as exc:
        # Refuse to serve rather than accept work we cannot store.
        out.log(f"FATAL: storage backend unusable -- {exc.message}")
        raise


def doctor(cfg: Config) -> dict:
    checks = {}
    for mod in ("sqlite3", "cv2", "numpy", "onnxruntime"):
        try:
            m = __import__(mod)
            checks[mod] = {"ok": True, "version": getattr(m, "__version__", "n/a")}
        except Exception as exc:
            checks[mod] = {"ok": False, "error": str(exc)}
    from . import resources as _res
    checks["resources"] = _res.describe()
    checks["resources"]["configured_workers"] = cfg.get("workers", "auto")
    checks["resources"]["effective_workers"] = _res.effective_workers(cfg)
    checks["resources"]["fits"] = _res.fits(_res.effective_workers(cfg))
    checks["models"] = {m["name"]: m["present"] for m in models.listing(cfg.models_dir)}
    checks["home"] = {"path": str(cfg.home), "exists": cfg.home.exists()}
    # The backend's own description, not a file path: on postgres or mongo
    # there is no file, and reporting one that will never exist reads as a
    # fault rather than as "this deployment does not use SQLite".
    try:
        from . import db as _db_check
        checks["db"] = dict(_db_check.dialect().describe())
        if checks["db"].get("backend") == "sqlite":
            checks["db"]["exists"] = Path(cfg.db_file).exists()
    except Exception as exc:
        checks["db"] = {"ok": False, "error": str(exc)}
    checks["dashboard_password_set"] = bool(cfg.get("dashboard_password"))
    ok = all(v["ok"] for k, v in checks.items() if isinstance(v, dict) and "ok" in v)
    ok = ok and all(checks["models"].values())
    return {"ok": ok, "python": sys.version.split()[0],
            "interpreter": sys.executable, "checks": checks,
            "next": [] if ok else ["blurd models pull --all",
                                   "pip install opencv-python-headless onnxruntime numpy"]}


def _job_human(job):
    lines = [f"job      {job['job_id']}  [{job['status']}]"
             + ("  (cached)" if job.get("cached") else "")]
    if job.get("external_id"):
        lines.append(f"code     {job['external_id']}")
    if job.get("source_sha"):
        lines.append(f"sha      {job['source_sha']}")
    res = job.get("result")
    if res:
        d = res["stats"]["detections"]
        lines += [
            f"profile  {res['profile_hash']}",
            f"found    {d['faces']} face(s), {d['plates']} plate(s)"
            + (f"  min_score={d['min_score']}" if d.get("min_score") else ""),
            f"review   {'YES - check this one' if res['needs_review'] else 'no'}",
            f"time     {job.get('duration_ms')} ms",
            f"blob     {res['blob'].get('path') or res['blob'].get('key', '')}",
        ]
    if job.get("error"):
        lines += [f"error    [{job['error']['code']}] {job['error']['message']}"]
        for s in job["error"].get("suggestions", []):
            lines.append(f"         - {s}")
    if job["status"] in ("queued", "running"):
        lines.append(f"poll     blurd jobs get {job['job_id']} --wait 30")
    return lines


def _jobs_human(res):
    q = res.get("queue", {})
    lines = [f"{res['count']} of {res['total']} job(s)   queue: "
             f"{q.get('queued', 0)} queued / {q.get('running', 0)} running / "
             f"{q.get('workers', '?')} workers", ""]
    for j in res["items"]:
        lines.append(f"  {j['job_id']}  {j['status']:<8} "
                     f"{(j.get('external_id') or '-'):<28} "
                     f"{(j.get('duration_ms') or 0):>8.0f} ms  {j['created_at']}")
    return lines


def _list_human(res):
    lines = [f"{res['count']} of {res['total']} artifact(s)", ""]
    for i in res["items"]:
        flag = "!" if i["needs_review"] else " "
        codes = ",".join(i.get("codes") or []) or "-"
        lines.append(f"{flag} {i['source_sha'][:16]}  {i['created_at']}  "
                     f"{i['n_faces']}f/{i['n_plates']}p  {i['width']}x{i['height']}  "
                     f"{codes}  [{','.join(i['tags'])}]")
    return lines


GLOBAL_FLAGS = {"--human", "--json", "--help-json"}
GLOBAL_OPTS = {"--remote", "--api-key", "--home"}


def _hoist_globals(argv):
    """argparse only accepts top-level flags before the subcommand. Agents (and
    humans) write `blurd blur x.jpg --human`, so move global flags to the front
    instead of rejecting the command."""
    head, rest, i = [], [], 0
    while i < len(argv):
        a = argv[i]
        if a in GLOBAL_FLAGS:
            head.append(a)
        elif a in GLOBAL_OPTS:
            head.append(a)
            if i + 1 < len(argv):
                i += 1
                head.append(argv[i])
        elif any(a.startswith(o + "=") for o in GLOBAL_OPTS):
            head.append(a)
        else:
            rest.append(a)
        i += 1
    return head + rest


def main(argv=None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(
            _hoist_globals(list(argv if argv is not None else sys.argv[1:])))
    except BlurdError as exc:
        Out().fail(exc.to_dict())
        return exc.code
    out = Out(human=args.human and not args.json)

    # help-json and guide emit their documents at the TOP level: a catalog
    # wrapped in the result envelope is not a catalog (cli-output-spec §4).
    if args.help_json or args.cmd == "help-json":
        json.dump(guide.as_json(), sys.stdout)
        sys.stdout.write("\n")
        return EXIT_SUCCESS
    if not args.cmd:
        print(guide.text(), file=sys.stderr)
        return 85

    try:
        return run(args, out)
    except BlurdError as exc:
        out.fail(exc.to_dict())
        return exc.code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return EXIT_GENERIC_FAILURE
    except Exception as exc:
        out.fail(Internal(f"Unhandled error: {exc}",
                          {"type": type(exc).__name__}).to_dict())
        return 110
