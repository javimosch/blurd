"""Canonical JSON + hashing.

PORTABILITY CONTRACT (spec/profile-hash.md): every blurd implementation must
produce byte-identical canonical JSON, so that a Go or machin rewrite computes
the same profile_hash and inherits the existing artifact cache instead of
invalidating every stored redaction.

Rules:
  - object keys sorted by unicode code point
  - no whitespace at all: separators are "," and ":"
  - UTF-8 output, non-ASCII characters emitted literally (no \\uXXXX escapes)
  - floats serialised with repr() semantics; profiles must only use values that
    round-trip exactly (thresholds are rounded to 3 decimals before hashing)
"""

import hashlib
import json
from typing import Any

PROFILE_HASH_LEN = 16


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def profile_hash(profile: dict) -> str:
    """Short, stable identity of a processing configuration."""
    return sha256_text(canonical_json(profile))[:PROFILE_HASH_LEN]
