"""Source acquisition: local file, raw bytes, or URL.

The URL path is the dangerous one. An API that fetches arbitrary URLs from
inside someone's VPC is a port scanner and a metadata-service reader unless it
is fenced in, so this module:

  * resolves the hostname itself and rejects private/loopback/link-local IPs
  * follows redirects MANUALLY, re-validating the IP at every hop (validating
    only the first URL is the classic DNS-rebind / redirect bypass)
  * caps bytes read, wall-clock time, and content type
"""

import ipaddress
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Tuple

from .errors import Upstream, ValidationError

ALLOWED_MIME = {
    "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
    "image/webp": ".webp", "image/bmp": ".bmp", "image/tiff": ".tif",
}


def _ip_is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return not (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def _validate_url(url: str, allow_private: bool) -> None:
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https"):
        raise ValidationError(
            f"Unsupported URL scheme '{parts.scheme}'",
            {"url": url}, ["Use an http:// or https:// URL"],
        )
    if not parts.hostname:
        raise ValidationError("URL has no host", {"url": url})
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror as exc:
        raise Upstream(f"DNS resolution failed for '{parts.hostname}': {exc}", {"url": url})
    for info in infos:
        ip = info[4][0]
        if not _ip_is_public(ip):
            raise ValidationError(
                f"Refusing to fetch a non-public address ({ip})",
                {"url": url, "host": parts.hostname, "resolved": ip},
                ["Set fetch.allow_private_ips=true in config to allow this (local testing only)"],
            )


def validate_url(url: str, cfg: dict) -> None:
    """Scheme + DNS + IP-range check, cheap enough to run synchronously at
    submit time. Without it an async API answers 202 to a URL it was never
    going to fetch, and the producer only finds out by polling."""
    _validate_url(url, bool(cfg.get("allow_private_ips", False)))


def fetch_url(url: str, cfg: dict) -> Tuple[bytes, str]:
    """Return (bytes, mime). Redirects are followed by hand, re-validating each hop."""
    allow_private = bool(cfg.get("allow_private_ips", False))
    timeout = float(cfg.get("timeout_s", 15))
    max_bytes = int(cfg.get("max_bytes", 5 * 1024 * 1024))
    max_redirects = int(cfg.get("max_redirects", 3))
    deadline = time.time() + timeout

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    current = url
    for _ in range(max_redirects + 1):
        _validate_url(current, allow_private)
        req = urllib.request.Request(
            current, headers={"User-Agent": cfg.get("user_agent", "blurd/0.1"),
                              "Accept": "image/*"})
        try:
            resp = opener.open(req, timeout=max(0.1, deadline - time.time()))
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                current = urllib.parse.urljoin(current, exc.headers["Location"])
                continue
            raise Upstream(f"HTTP {exc.code} fetching image", {"url": current, "status": exc.code},
                           retry_after=5 if exc.code >= 500 else None)
        except Exception as exc:
            raise Upstream(f"Failed to fetch image: {exc}", {"url": current})

        with resp:
            mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > max_bytes:
                raise ValidationError(
                    f"Image too large: {declared} bytes > limit {max_bytes}",
                    {"url": current, "max_bytes": max_bytes})
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise ValidationError(
                f"Image exceeds max_bytes ({max_bytes})", {"url": current})
        if mime and mime not in ALLOWED_MIME and not mime.startswith("image/"):
            raise ValidationError(
                f"Unsupported content type '{mime}'",
                {"url": current, "allowed": sorted(ALLOWED_MIME)})
        return data, mime or "application/octet-stream"

    raise ValidationError(f"Too many redirects (> {max_redirects})", {"url": url})


def read_file(path: str, cfg: dict) -> Tuple[bytes, str]:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ValidationError(f"No such file: {path}", {"path": str(p)})
    size = p.stat().st_size
    max_bytes = int(cfg.get("max_bytes", 5 * 1024 * 1024))
    if size > max_bytes:
        raise ValidationError(f"File too large: {size} > {max_bytes}", {"path": str(p)})
    data = p.read_bytes()
    return data, sniff_mime(data)


def sniff_mime(data: bytes) -> str:
    """Trust the bytes, not the Content-Type header."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:2] == b"BM":
        return "image/bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    return "application/octet-stream"
