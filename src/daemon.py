"""Daemon lifecycle: start / stop / status, per cli-daemon-spec."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import db
from .config import Config
from .errors import Conflict, Internal


class Daemon:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _pid(self):
        try:
            return int(Path(self.cfg.pid_file).read_text().strip())
        except (OSError, ValueError):
            return None

    def is_running(self) -> bool:
        pid = self._pid()
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            Path(self.cfg.pid_file).unlink(missing_ok=True)
            return False
        except PermissionError:
            return True

    def start(self, host: str, port: int) -> dict:
        if self.is_running():
            # cli-daemon-spec: a second `daemon start` reports the running
            # instance and succeeds -- the desired end state already holds.
            return {"status": "running", "already_running": True,
                    "pid": self._pid(),
                    "pid_file": str(self.cfg.pid_file),
                    "host": self.cfg.get("host"), "port": self.cfg.get("port")}
        if self._port_open(host, port):
            # The readiness check below cannot tell our child from a stranger,
            # so a port already in use would otherwise be reported as a
            # successful start -- and the caller would talk to the wrong
            # process, quite possibly one running older code.
            raise Conflict(
                f"Port {port} is already in use",
                {"host": host, "port": port,
                 "hint": "another blurd (or something else) is listening; "
                         "stop it, or start on a different --port"})
        self.cfg.ensure_dirs()
        entry = Path(__file__).resolve().parent.parent / "run.py"
        log = open(self.cfg.log_file, "a")
        proc = subprocess.Popen(
            [sys.executable, str(entry), "serve", "--host", host, "--port", str(port),
             "--foreground"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,      # detach from the controlling terminal
        )
        Path(self.cfg.pid_file).write_text(str(proc.pid))
        # Wait for the port to actually answer rather than claiming success on
        # a process that will die in 200ms with a bind error.
        for _ in range(60):
            time.sleep(0.1)
            if proc.poll() is not None:
                Path(self.cfg.pid_file).unlink(missing_ok=True)
                raise Internal("Daemon exited immediately after start",
                               {"log_file": str(self.cfg.log_file),
                                "exit_code": proc.returncode})
            if self._port_open(host, port):
                return {"status": "running", "mode": "daemon", "pid": proc.pid,
                        "host": host, "port": port,
                        "url": f"http://{host}:{port}",
                        "log_file": str(self.cfg.log_file)}
        raise Internal("Daemon did not become healthy within 6s",
                       {"log_file": str(self.cfg.log_file), "pid": proc.pid})

    @staticmethod
    def _port_open(host, port) -> bool:
        import socket
        with socket.socket() as s:
            s.settimeout(0.3)
            return s.connect_ex((host if host != "0.0.0.0" else "127.0.0.1", port)) == 0

    def stop(self, timeout: float = 5.0) -> dict:
        if not self.is_running():
            # cli-daemon-spec: stopping a stopped daemon is a no-op success,
            # not an error -- the desired end state already holds.
            return {"status": "not_running",
                    "pid_file": str(self.cfg.pid_file)}
        pid = self._pid()
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
        else:
            os.kill(pid, signal.SIGKILL)
        Path(self.cfg.pid_file).unlink(missing_ok=True)
        return {"status": "stopped", "pid": pid}

    def status(self) -> dict:
        if not self.is_running():
            return {"status": "not_running", "pid_file": str(self.cfg.pid_file)}
        conn = db.connect(self.cfg.db_file) if Path(self.cfg.db_file).exists() else None
        out = {"status": "running", "pid": self._pid(),
               "home": str(self.cfg.home),
               "log_file": str(self.cfg.log_file),
               "port": self.cfg.get("port"), "host": self.cfg.get("host")}
        if conn:
            out["counts"] = db.stats(conn)
        return out
