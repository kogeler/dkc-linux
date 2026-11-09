"""Crash-injection model for the signed repository commit protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable, Protocol

from .storage import ObjectMetadata
from .storage_progress import StoragePhase, StorageProgressEvent

__all__ = [
    "InjectedFailure",
    "PublicationExecutor",
    "PublicationObject",
    "PublicationPlan",
    "PublicationTransfers",
]


@dataclass(frozen=True)
class PublicationObject:
    body: bytes
    metadata: ObjectMetadata


@dataclass(frozen=True)
class PublicationPlan:
    immutable: Mapping[str, PublicationObject]
    mutable_before_commit: Mapping[str, PublicationObject]
    inrelease: PublicationObject
    state_pointer: PublicationObject
    conveniences: Mapping[str, PublicationObject]
    mutable_preconditions: Mapping[str, str | None]
    transaction_key: str
    inrelease_key: str = "dists/trixie/InRelease"
    state_key: str = "state/current.asc"

    def __post_init__(self) -> None:
        mutable = (
            set(self.mutable_before_commit)
            | {self.inrelease_key, self.state_key}
            | set(self.conveniences)
        )
        if set(self.mutable_preconditions) != mutable:
            raise ValueError("every mutable key needs exactly one captured precondition")
        all_keys = set(self.immutable) | mutable
        if len(all_keys) != len(self.immutable) + len(mutable):
            raise ValueError("immutable and mutable publication keys overlap")
        if self.transaction_key not in self.immutable:
            raise ValueError("the signed transaction record must be immutable input")


class InjectedFailure(RuntimeError):
    def __init__(self, phase: int) -> None:
        super().__init__(f"injected failure after publication phase {phase}")
        self.phase = phase


class PublicationStore(Protocol):
    def get(self, key: str) -> PublicationValue | None: ...

    def head(self, key: str, *, if_match: str) -> PublicationHead: ...

    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> PublicationValue: ...

    def create_immutable(
        self, key: str, body: bytes, metadata: ObjectMetadata
    ) -> PublicationValue: ...


class PublicationValue(Protocol):
    @property
    def body(self) -> bytes: ...

    @property
    def metadata(self) -> ObjectMetadata: ...

    @property
    def etag(self) -> str: ...


class PublicationHead(Protocol):
    @property
    def size(self) -> int: ...

    @property
    def metadata(self) -> ObjectMetadata: ...

    @property
    def etag(self) -> str: ...


@dataclass(frozen=True)
class PublicationTransfers:
    created_objects: int
    created_bytes: int
    reused_objects: int
    reused_bytes: int


class PublicationExecutor:
    """Execute the 12-phase client/state protocol against conditional storage."""

    def __init__(self, store: PublicationStore) -> None:
        self.store = store

    @staticmethod
    def _inject(phase: int, fail_after: int | None) -> None:
        if phase == fail_after:
            raise InjectedFailure(phase)

    def _put_mutable(
        self, key: str, value: PublicationObject, precondition: str | None
    ) -> None:
        current = self.store.get(key)
        if current is not None and current.body == value.body and current.metadata == value.metadata:
            return
        if precondition is None:
            self.store.put(key, value.body, value.metadata, if_none_match=True)
        else:
            self.store.put(key, value.body, value.metadata, if_match=precondition)

    def _verify(self, key: str, value: PublicationObject) -> None:
        current = self.store.get(key)
        if current is None or current.body != value.body or current.metadata != value.metadata:
            raise RuntimeError(f"origin verification failed for {key}")

    def _prepare_immutable(self, key: str, value: PublicationObject) -> str | None:
        # Reuse requires exact bytes before the client commit. Do not send an
        # existing multi-gigabyte object merely to discover a 412 response.
        current = self.store.get(key)
        if current is not None:
            if current.body != value.body or current.metadata != value.metadata:
                raise RuntimeError(f"immutable object collision for {key}")
            return current.etag
        self.store.create_immutable(key, value.body, value.metadata)
        return None

    def _revalidate_immutable(
        self, key: str, value: PublicationObject, etag: str
    ) -> None:
        # ETag is an opaque validator for bytes read during this execution,
        # never a digest or evidence carried over from an earlier publication.
        current = self.store.head(key, if_match=etag)
        if (
            current.etag != etag
            or current.size != len(value.body)
            or current.metadata != value.metadata
        ):
            raise RuntimeError(f"origin revalidation failed for {key}")

    def execute(
        self,
        plan: PublicationPlan,
        *,
        fail_after: int | None = None,
        mutation_checkpoint: Callable[[], None] | None = None,
        progress: Callable[[StorageProgressEvent], None] | None = None,
    ) -> PublicationTransfers:
        if fail_after is not None and not 1 <= fail_after <= 12:
            raise ValueError("failure phase must be between 1 and 12")

        # Planning, canonical inventory, validation, and signing happen before
        # storage credentials are attached. They are represented explicitly so
        # every documented crash boundary is covered by the same test matrix.
        for phase in range(1, 5):
            self._inject(phase, fail_after)

        def report(
            phase: StoragePhase,
            objects: int = 0,
            total_objects: int = 0,
            size: int = 0,
            total_size: int = 0,
        ) -> None:
            if progress is not None:
                progress(StorageProgressEvent(phase, objects, total_objects, size, total_size))

        reused: dict[str, str] = {}
        immutable_size = sum(len(value.body) for value in plan.immutable.values())
        prepared_objects = 0
        prepared_bytes = 0

        def prepare(key: str, value: PublicationObject) -> None:
            nonlocal prepared_objects, prepared_bytes
            if mutation_checkpoint is not None:
                mutation_checkpoint()
            etag = self._prepare_immutable(key, value)
            if etag is not None:
                reused[key] = etag
            if mutation_checkpoint is not None:
                mutation_checkpoint()
            prepared_objects += 1
            prepared_bytes += len(value.body)
            report(
                StoragePhase.IMMUTABLE, prepared_objects, len(plan.immutable),
                prepared_bytes, immutable_size,
            )

        report(StoragePhase.IMMUTABLE, total_objects=len(plan.immutable), total_size=immutable_size)
        transaction = plan.immutable[plan.transaction_key]
        prepare(plan.transaction_key, transaction)
        self._inject(5, fail_after)

        for key, value in sorted(plan.immutable.items()):
            if key == plan.transaction_key:
                continue
            prepare(key, value)
        self._inject(6, fail_after)

        report(StoragePhase.METADATA)
        for key, value in sorted(plan.mutable_before_commit.items()):
            if mutation_checkpoint is not None:
                mutation_checkpoint()
            self._put_mutable(key, value, plan.mutable_preconditions[key])
            if mutation_checkpoint is not None:
                mutation_checkpoint()
        self._inject(7, fail_after)

        report(StoragePhase.COMMIT_CLIENT)
        if mutation_checkpoint is not None:
            mutation_checkpoint()
        self._put_mutable(
            plan.inrelease_key,
            plan.inrelease,
            plan.mutable_preconditions[plan.inrelease_key],
        )
        if mutation_checkpoint is not None:
            mutation_checkpoint()
        self._inject(8, fail_after)

        verify_total = len(plan.immutable) + len(plan.mutable_before_commit) + 1
        verify_bytes = immutable_size + sum(
            len(value.body) for value in plan.mutable_before_commit.values()
        ) + len(plan.inrelease.body)
        verified_objects = 0
        verified_bytes = 0
        report(StoragePhase.VERIFY_REPOSITORY, total_objects=verify_total, total_size=verify_bytes)
        for key, value in (
            list(plan.immutable.items())
            + list(plan.mutable_before_commit.items())
            + [(plan.inrelease_key, plan.inrelease)]
        ):
            if key in reused:
                self._revalidate_immutable(key, value, reused[key])
            else:
                self._verify(key, value)
            verified_objects += 1
            verified_bytes += len(value.body)
            report(
                StoragePhase.VERIFY_REPOSITORY, verified_objects, verify_total,
                verified_bytes, verify_bytes,
            )
        self._inject(9, fail_after)

        report(StoragePhase.CONVENIENCES)
        for key, value in sorted(
            plan.conveniences.items(),
            key=lambda item: (not item[0].endswith(".asc"), item[0]),
        ):
            if mutation_checkpoint is not None:
                mutation_checkpoint()
            self._put_mutable(key, value, plan.mutable_preconditions[key])
            if mutation_checkpoint is not None:
                mutation_checkpoint()
        self._inject(10, fail_after)

        # Controller authority moves last. Once this CAS succeeds there is no
        # remaining publication mutation that a later no-op run could skip.
        report(StoragePhase.COMMIT_STATE)
        if mutation_checkpoint is not None:
            mutation_checkpoint()
        self._put_mutable(
            plan.state_key,
            plan.state_pointer,
            plan.mutable_preconditions[plan.state_key],
        )
        if mutation_checkpoint is not None:
            mutation_checkpoint()
        self._inject(11, fail_after)

        report(StoragePhase.VERIFY_STATE)
        for key, value in plan.mutable_before_commit.items():
            self._verify(key, value)
        self._verify(plan.inrelease_key, plan.inrelease)
        self._verify(plan.state_key, plan.state_pointer)
        for key, value in plan.conveniences.items():
            self._verify(key, value)
        self._inject(12, fail_after)
        reused_bytes = sum(len(plan.immutable[key].body) for key in reused)
        return PublicationTransfers(
            len(plan.immutable) - len(reused), immutable_size - reused_bytes,
            len(reused), reused_bytes,
        )
