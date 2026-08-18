"""Exact in-memory storage model used only by unit tests."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

from dkc.storage import ObjectMetadata


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_key(value: str) -> None:
    parts = value.split("/")
    if (
        not value
        or value.startswith("/")
        or value.endswith("/")
        or "*" in value
        or "\\" in value
        or _CONTROL_RE.search(value)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError(f"unsafe object key: {value!r}")


@dataclass(frozen=True)
class StoredObject:
    body: bytes
    metadata: ObjectMetadata
    etag: str


class PreconditionFailed(RuntimeError):
    """The semantic equivalent of an HTTP 412 response."""


class ConditionalObjectStore:
    """Thread-safe model of conditional single-object writes."""

    def __init__(self) -> None:
        self._objects: dict[str, StoredObject] = {}
        self._revision = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> StoredObject | None:
        _validate_key(key)
        with self._lock:
            return self._objects.get(key)

    def keys(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._objects))

    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> StoredObject:
        _validate_key(key)
        if if_none_match == (if_match is not None):
            raise ValueError("a write requires exactly one conditional precondition")
        with self._lock:
            current = self._objects.get(key)
            if if_none_match:
                if current is not None:
                    raise PreconditionFailed(f"If-None-Match lost for {key}")
            elif current is None or current.etag != if_match:
                raise PreconditionFailed(f"If-Match lost for {key}")
            self._revision += 1
            result = StoredObject(
                body=bytes(body),
                metadata=metadata,
                etag=f'"local-revision-{self._revision}"',
            )
            self._objects[key] = result
            return result

    def create_immutable(
        self, key: str, body: bytes, metadata: ObjectMetadata
    ) -> StoredObject:
        try:
            return self.put(key, body, metadata, if_none_match=True)
        except PreconditionFailed:
            current = self.get(key)
            if current is None or current.body != body or current.metadata != metadata:
                raise PreconditionFailed(f"immutable object collision for {key}") from None
            return current

    def delete(self, key: str) -> None:
        _validate_key(key)
        with self._lock:
            self._objects.pop(key, None)
