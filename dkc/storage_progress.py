"""Structured live progress outside the buffered, secret-bearing output stream."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TextIO

from .storage_output import StorageRedactor


class StoragePhase(StrEnum):
    VALIDATE = "validate-repository"
    ACQUIRE_LEASE = "acquire-lease"
    PLAN = "plan-publication"
    PLAN_GC = "plan-garbage-collection"
    PROJECT = "project-storage-size"
    IMMUTABLE = "prepare-immutable-objects"
    METADATA = "publish-indexes"
    COMMIT_CLIENT = "commit-apt-view"
    VERIFY_REPOSITORY = "verify-repository"
    CONVENIENCES = "publish-root-metadata"
    COMMIT_STATE = "commit-controller-state"
    VERIFY_STATE = "verify-final-metadata"
    GC = "collect-retired-objects"
    RELEASE_LEASE = "release-lease"
    VERIFY_STORAGE = "verify-storage-size"


@dataclass(frozen=True)
class StorageProgressEvent:
    phase: StoragePhase
    objects: int = 0
    total_objects: int = 0
    bytes: int = 0
    total_bytes: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.phase, StoragePhase) or any(
            type(value) is not int or value < 0
            for value in (
                self.objects,
                self.total_objects,
                self.bytes,
                self.total_bytes,
            )
        ):
            raise ValueError(
                "progress accepts only fixed phases and nonnegative counters"
            )
        if self.objects > self.total_objects or self.bytes > self.total_bytes:
            raise ValueError("progress exceeds its total")


class StorageProgress:
    """Flush complete sanitized records; never forward arbitrary command output."""

    def __init__(
        self,
        redactor: StorageRedactor,
        stream: TextIO,
        *,
        clock: Callable[[], float] = time.monotonic,
        interval_seconds: float = 15,
    ) -> None:
        self.redactor = redactor
        self.stream = stream
        self.clock = clock
        self.interval_seconds = interval_seconds
        self.started = clock()
        self.phase_started = self.started
        self.last_emitted = self.started
        self.current: StorageProgressEvent | None = None
        self.phases: list[dict[str, str | int | float]] = []

    def _record_phase(self, now: float) -> dict[str, str | int | float]:
        assert self.current is not None
        return {
            "phase": self.current.phase.value,
            "elapsed_seconds": round(now - self.phase_started, 3),
            "objects": self.current.objects,
            "total_objects": self.current.total_objects,
            "bytes": self.current.bytes,
            "total_bytes": self.current.total_bytes,
        }

    def __call__(self, event: StorageProgressEvent) -> None:
        now = self.clock()
        changed = self.current is None or self.current.phase != event.phase
        if changed:
            if self.current is not None:
                self.phases.append(self._record_phase(now))
            self.phase_started = now
        self.current = event
        if not (
            changed
            or event.objects == event.total_objects
            or now - self.last_emitted >= self.interval_seconds
        ):
            return
        line = self.redactor.redact(
            f"storage phase={event.phase.value} "
            f"objects={event.objects}/{event.total_objects} "
            f"bytes={event.bytes}/{event.total_bytes} "
            f"elapsed_seconds={now - self.started:.1f}\n"
        )
        if self.redactor.contains_connection_value(line):
            raise RuntimeError("storage progress sanitizer invariant")
        self.stream.write(line)
        self.stream.flush()
        self.last_emitted = now

    def summary(self) -> list[dict[str, str | int | float]]:
        return self.phases + (
            [self._record_phase(self.clock())] if self.current is not None else []
        )
