"""Deterministic JSON serialization.

Signed state, publication manifests, and transaction records are compared by
hash and signed as bytes, so two runs that mean the same thing must produce the
same bytes. Python's default `json.dumps` does not guarantee that: key order,
separators, and non-ASCII escaping all vary with call site.

Every record this project signs or hashes goes through `dumps` here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = [
    "boolean_text",
    "canonical_bytes",
    "dumps",
    "loads",
    "parse_boolean_text",
    "sha256_of",
]


def boolean_text(value: bool) -> str:
    """Return the one lowercase representation used by workflow handoffs."""

    if not isinstance(value, bool):
        raise TypeError("workflow boolean must be a bool")
    return "true" if value else "false"


def parse_boolean_text(value: str) -> bool:
    """Parse the exact lowercase representation used by workflow handoffs."""

    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("workflow boolean must be exactly true or false")


def dumps(value: Any) -> str:
    """Serialize to canonical JSON text.

    Rules, chosen so the output is stable and diffable:
      - keys sorted, so insertion order cannot leak into the bytes;
      - compact separators, so whitespace cannot vary;
      - UTF-8 output rather than \\u escapes, so text stays readable;
      - a trailing newline, so the file is a well-formed text file.

    Floats are rejected: their repr is platform-sensitive and no record in this
    project needs one. Sizes and timestamps are integers or strings.
    """
    _reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"


def canonical_bytes(value: Any) -> bytes:
    return dumps(value).encode("utf-8")


def loads(text: str | bytes) -> Any:
    return json.loads(text)


def sha256_of(value: Any) -> str:
    """SHA-256 of the canonical serialization, as lowercase hex."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _reject_floats(value: Any, path: str = "$") -> None:
    if isinstance(value, float):
        raise TypeError(f"float at {path} is not allowed in a canonical record")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string key at {path}: {key!r}")
            _reject_floats(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, f"{path}[{index}]")
