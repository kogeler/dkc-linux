"""Disposable object-layout and conditional-write qualification."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .s3 import PreconditionFailed, RemoteObject
from .storage import ObjectMetadata
from .storage_repository import (
    MUTABLE_CACHE,
    RepositoryObject,
    build_disposable_prefix,
    validate_disposable_prefix,
)

__all__ = [
    "DisposableConfig",
    "DisposableIntegration",
    "IntegrationEvent",
    "cleanup_disposable_prefix",
]


class Store(Protocol):
    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str: ...

    def get(self, key: str) -> RemoteObject: ...

    def delete(self, key: str) -> None: ...

    def list_keys(
        self, prefix: str, *, page_size: int | None = None
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, repr=False)
class DisposableConfig:
    canonical_repository: str
    run_id: str
    nonce: str

    def __post_init__(self) -> None:
        build_disposable_prefix(self.canonical_repository, self.run_id, self.nonce)

    @property
    def prefix(self) -> str:
        return build_disposable_prefix(
            self.canonical_repository, self.run_id, self.nonce
        )


@dataclass(frozen=True)
class IntegrationEvent:
    operation: str
    key: str
    status: str
    detail: str = ""


def cleanup_disposable_prefix(
    store: Store, prefix: str
) -> tuple[IntegrationEvent, ...]:
    validate_disposable_prefix(prefix)
    events: list[IntegrationEvent] = []
    for key in store.list_keys(prefix):
        if not key.startswith(prefix):
            raise RuntimeError("cleanup listing escaped the disposable prefix")
        store.delete(key)
        events.append(IntegrationEvent("cleanup-delete", key, "2xx"))
    leftovers = store.list_keys(prefix)
    if leftovers:
        raise RuntimeError(f"disposable prefix cleanup left {len(leftovers)} objects")
    events.append(IntegrationEvent("final-list", prefix, "PASS", "zero objects"))
    return tuple(events)


class DisposableIntegration:
    """Exercise S3 semantics and clean one exact-prefix fixture."""

    def __init__(self, store: Store, config: DisposableConfig) -> None:
        self.store = store
        self.config = config
        self.events: list[IntegrationEvent] = []

    def _record(self, operation: str, key: str, status: str, detail: str = "") -> None:
        self.events.append(IntegrationEvent(operation, key, status, detail))

    @staticmethod
    def _verify_remote(
        key: str,
        expected_body: bytes,
        expected_metadata: ObjectMetadata,
        remote: RemoteObject,
    ) -> None:
        if remote.body != expected_body:
            raise RuntimeError(f"authoritative body verification failed for {key}")
        if remote.metadata != expected_metadata:
            raise RuntimeError(
                f"authoritative HTTP metadata verification failed for {key}"
            )

    def _upload_repository(self, inventory: Sequence[RepositoryObject]) -> None:
        if not inventory:
            raise ValueError("verified repository inventory is empty")
        for item in inventory:
            key = f"{self.config.prefix}repository/{item.relative_key}"
            body = item.read()
            self.store.put(key, body, item.metadata, if_none_match=True)
            self._record("conditional-create", key, "2xx", item.sha256)
            remote = self.store.get(key)
            self._verify_remote(key, body, item.metadata, remote)
            if hashlib.sha256(remote.body).hexdigest() != item.sha256:
                raise RuntimeError(f"authoritative digest verification failed for {key}")
            self._record("authoritative-read", key, "PASS", item.sha256)
        expected = tuple(
            sorted(
                f"{self.config.prefix}repository/{item.relative_key}"
                for item in inventory
            )
        )
        listed = self.store.list_keys(self.config.prefix, page_size=10)
        if listed != expected:
            raise RuntimeError("paginated repository listing differs from upload inventory")
        self._record(
            "paginated-list",
            self.config.prefix,
            "PASS",
            f"objects={len(listed)} page_size=10",
        )

    def _cas_probe(self) -> None:
        key = f"{self.config.prefix}checks/cas.json"
        metadata = ObjectMetadata("application/json", MUTABLE_CACHE)
        initial = b'{"revision":0}\n'
        etag = self.store.put(key, initial, metadata, if_none_match=True)
        self._record("cas-create", key, "2xx")
        barrier = threading.Barrier(3)
        lock = threading.Lock()
        outcomes: list[tuple[str, bytes]] = []
        failures: list[BaseException] = []

        def update(body: bytes) -> None:
            barrier.wait()
            try:
                self.store.put(key, body, metadata, if_match=etag)
            except PreconditionFailed:
                with lock:
                    outcomes.append(("412", body))
            except BaseException as exc:
                with lock:
                    failures.append(exc)
            else:
                with lock:
                    outcomes.append(("2xx", body))

        bodies = (
            b'{"revision":1,"writer":"one"}\n',
            b'{"revision":1,"writer":"two"}\n',
        )
        threads = [threading.Thread(target=update, args=(body,)) for body in bodies]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        if failures:
            raise RuntimeError(f"CAS writer failed: {failures[0]}") from failures[0]
        statuses = sorted(status for status, _ in outcomes)
        if statuses != ["2xx", "412"]:
            raise RuntimeError(f"CAS race produced unsafe outcomes: {statuses}")
        winning = next(body for status, body in outcomes if status == "2xx")
        remote = self.store.get(key)
        self._verify_remote(key, winning, metadata, remote)
        self._record("cas-race", key, "PASS", "one 2xx and one 412")

    def _cleanup(self) -> None:
        self.events.extend(cleanup_disposable_prefix(self.store, self.config.prefix))

    def run(self, inventory: Sequence[RepositoryObject]) -> tuple[IntegrationEvent, ...]:
        preexisting = self.store.list_keys(self.config.prefix)
        if preexisting:
            raise RuntimeError("disposable prefix was not empty before mutation")
        self._record("preflight-list", self.config.prefix, "PASS", "zero objects")
        primary_error: BaseException | None = None
        try:
            self._upload_repository(inventory)
            self._cas_probe()
        except BaseException as exc:
            primary_error = exc
        try:
            self._cleanup()
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            raise RuntimeError(
                f"integration failed ({primary_error}); cleanup also failed ({cleanup_error})"
            ) from cleanup_error
        if primary_error is not None:
            raise primary_error
        return tuple(self.events)
