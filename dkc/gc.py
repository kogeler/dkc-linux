"""Signed-tombstone-driven exact garbage collection."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from .records import GcPlan, GcTarget, PublicationManifest
from .storage import ObjectMetadata

__all__ = ["execute_gc", "plan_gc"]


class ObjectValue(Protocol):
    @property
    def body(self) -> bytes: ...

    @property
    def metadata(self) -> ObjectMetadata: ...


class GcStore(Protocol):
    def get(self, key: str) -> ObjectValue | None: ...

    def delete(self, key: str) -> None: ...


def _utc_now(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("GC clock must return an aware datetime")
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _matches(value: ObjectValue, target: GcTarget) -> bool:
    return (
        len(value.body) == target.size
        and hashlib.sha256(value.body).hexdigest() == target.sha256
        and value.metadata.cache_control.endswith("immutable")
    )


def plan_gc(
    manifest: PublicationManifest,
    store: GcStore,
    *,
    now: datetime,
    max_objects: int,
    max_bytes: int,
) -> GcPlan:
    """Build a complete bounded plan from permanent tombstones.

    Missing targets are already complete and therefore omitted. Existing bytes
    must still match the signed retirement record exactly before they can enter
    a deletion plan.
    """

    if max_objects < 1 or max_bytes < 1:
        raise ValueError("GC caps must be positive")
    now_utc = _utc_now(now)
    queued = {
        entry.key: entry
        for entry in manifest.gc_queue
        if entry.key not in set(manifest.live_objects)
    }
    targets: list[GcTarget] = []
    total = 0
    for key, entry in sorted(queued.items()):
        candidate = GcTarget(
            key=key,
            sha256=entry.sha256,
            reason=entry.reason,
            size=entry.size,
        )
        if candidate.size > max_bytes:
            raise ValueError("one GC target exceeds the configured byte cap")
        current = store.get(key)
        if current is None:
            continue
        if not _matches(current, candidate):
            raise ValueError("a GC target differs from its signed tombstone")
        if len(targets) >= max_objects or total + candidate.size > max_bytes:
            raise ValueError("the complete GC plan exceeds the configured safety caps")
        targets.append(candidate)
        total += candidate.size
    return GcPlan(
        expected_generation=manifest.generation,
        targets=targets,
        caps={"max_objects": max_objects, "max_bytes": max_bytes},
        planned_utc=now_utc,
    )


def execute_gc(
    plan: GcPlan,
    store: GcStore,
    *,
    observed_generation: Callable[[], int],
    mutation_checkpoint: Callable[[], None],
) -> tuple[str, ...]:
    """Revalidate generation, bytes, and lease around every exact deletion."""

    deleted: list[str] = []
    for target in plan.targets:
        if observed_generation() != plan.expected_generation:
            raise RuntimeError("authoritative generation changed before GC deletion")
        current = store.get(target.key)
        if current is None:
            continue
        if not _matches(current, target):
            raise RuntimeError("GC target changed after planning")
        mutation_checkpoint()
        store.delete(target.key)
        mutation_checkpoint()
        if store.get(target.key) is not None:
            raise RuntimeError("GC target still exists after deletion")
        if observed_generation() != plan.expected_generation:
            raise RuntimeError("authoritative generation changed during GC deletion")
        deleted.append(target.key)
    return tuple(deleted)
