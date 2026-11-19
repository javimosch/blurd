"""The client's address, as far as blurd can trust it.

Behind an ingress (k8s), Traefik (Coolify) or nginx, the TCP peer is the proxy,
so every caller looks the same: one rate-limit bucket, one address in the audit
trail. Proxies report the real caller in X-Forwarded-For, but any client can
send that header too -- so it is believed ONLY when the TCP peer is a proxy the
operator listed in `trusted_proxies`.

The header is read right to left: each trusted proxy appends the address it
received the request from, so the first address that is NOT a trusted proxy is
the client. Taking the leftmost entry instead would let a caller pick its own
address by sending `X-Forwarded-For: <anything>` through an honest proxy.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Optional, Tuple


@lru_cache(maxsize=8)
def parse_trusted(raw: str) -> Tuple:
    """'10.0.0.0/8, 192.168.1.5' -> networks. Bad entries raise ValueError at
    startup rather than silently trusting nothing (or everything)."""
    nets = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))
    return tuple(nets)


def _ip(s: str):
    s = (s or "").strip().strip('"')
    if s.startswith("[") and "]" in s:          # [v6]:port
        s = s[1:s.index("]")]
    elif s.count(":") == 1:                      # v4:port
        s = s.split(":", 1)[0]
    try:
        return ipaddress.ip_address(s)
    except ValueError:
        return None


def _trusted(addr, nets) -> bool:
    return addr is not None and any(addr in n for n in nets)


def resolve(peer: str, forwarded_for: Optional[str], trusted_raw: str) -> str:
    nets = parse_trusted(trusted_raw)
    if not nets or not forwarded_for:
        return peer
    if not _trusted(_ip(peer), nets):
        return peer                              # header from a stranger: ignore
    hops = [h for h in forwarded_for.split(",") if h.strip()]
    for hop in reversed(hops):
        addr = _ip(hop)
        if addr is None:
            # Garbage in the chain: stop at the last address we could verify
            # rather than skip past it to something a caller injected.
            return peer
        if not _trusted(addr, nets):
            return str(addr)
        peer = str(addr)
    return peer                                  # every hop is a proxy
