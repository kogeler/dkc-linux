"""Strict parsing of the signed authoritative repository state graph."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from .records import (
    Artifact,
    GcQueueEntry,
    PublicationManifest,
    StatePointer,
)
from .schema import validate
from .serialize import loads

__all__ = ["AuthoritativeState", "parse_manifest", "parse_state_pointer"]


def _object(body: bytes, name: str) -> dict[str, Any]:
    try:
        value = loads(body)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{name} is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} is not a JSON object")
    return cast(dict[str, Any], value)


def parse_state_pointer(body: bytes) -> StatePointer:
    value = _object(body, "state pointer")
    validate("state-pointer", value)
    return StatePointer(**value)


def parse_manifest(body: bytes) -> PublicationManifest:
    value = _object(body, "publication manifest")
    validate("publication-manifest", value)
    raw_artifacts = value.pop("artifacts")
    raw_queue = value.pop("gc_queue", [])
    if not isinstance(raw_artifacts, list) or not isinstance(raw_queue, list):
        raise ValueError("publication manifest collections are malformed")
    artifacts = [Artifact(**item) for item in raw_artifacts]
    queue = [GcQueueEntry(**item) for item in raw_queue]
    return PublicationManifest(artifacts=artifacts, gc_queue=queue, **value)


@dataclass(frozen=True)
class AuthoritativeState:
    pointer: StatePointer
    manifest: PublicationManifest
    state_etag: str
    manifest_etag: str
    storage_object_count: int = 0
    storage_size: int = 0

    def __post_init__(self) -> None:
        if self.pointer.generation != self.manifest.generation:
            raise ValueError("state pointer and manifest generations differ")
        if self.pointer.publication_id != self.manifest.publication_id:
            raise ValueError("state pointer and manifest publication IDs differ")
        if not self.state_etag or not self.manifest_etag:
            raise ValueError("authoritative state requires opaque object revisions")
        if self.storage_object_count < 0 or self.storage_size < 0:
            raise ValueError("authoritative storage inventory must not be negative")
