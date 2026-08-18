"""Exact whole-namespace byte accounting before an object-store commit."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

__all__ = ["StorageProjection", "project_storage"]


class SizedKey(Protocol):
    @property
    def key(self) -> str: ...

    @property
    def size(self) -> int: ...


class SizedWrite(Protocol):
    @property
    def relative_key(self) -> str: ...

    @property
    def size(self) -> int: ...


@dataclass(frozen=True)
class StorageProjection:
    object_count: int
    size: int

    def __post_init__(self) -> None:
        if self.object_count < 0 or self.size < 0:
            raise ValueError("storage projection must not be negative")


def project_storage(
    current: Iterable[SizedKey],
    writes: Iterable[SizedWrite],
    deletions: Iterable[str],
) -> StorageProjection:
    """Apply exact intended writes and deletions to one listed namespace."""

    objects: dict[str, int] = {}
    for current_item in current:
        if current_item.key in objects or current_item.size < 0:
            raise ValueError("current storage inventory is malformed")
        objects[current_item.key] = current_item.size
    write_keys: set[str] = set()
    for write in writes:
        if write.relative_key in write_keys or write.size < 0:
            raise ValueError("publication storage inventory is malformed")
        write_keys.add(write.relative_key)
        objects[write.relative_key] = write.size
    deletion_values = tuple(deletions)
    deletion_keys = set(deletion_values)
    if len(deletion_keys) != len(deletion_values):
        raise ValueError("storage deletion inventory repeats a key")
    overlap = write_keys & deletion_keys
    if overlap:
        raise ValueError("storage projection would both write and delete an object")
    for key in deletion_keys:
        objects.pop(key, None)
    return StorageProjection(len(objects), sum(objects.values()))
