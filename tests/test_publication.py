from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from dkc.publication import (
    InjectedFailure,
    PublicationExecutor,
    PublicationObject,
    PublicationPlan,
)
from dkc.storage import ObjectMetadata
from dkc.storage_progress import StoragePhase
from tests.fake_storage import ConditionalObjectStore, PreconditionFailed

IMMUTABLE = ObjectMetadata(
    content_type="application/octet-stream",
    cache_control="public, max-age=31536000, immutable",
)
MUTABLE = ObjectMetadata(
    content_type="text/plain",
    cache_control="public, max-age=0, must-revalidate",
)


def _object(body: str, metadata: ObjectMetadata = MUTABLE) -> PublicationObject:
    return PublicationObject(body.encode(), metadata)


def _plan() -> PublicationPlan:
    transaction = "state/transactions/20260813-abcdef12/record.json"
    precommit = {"dists/trixie/Release": _object("release")}
    conveniences = {
        "manifest.json": _object("manifest"),
        "manifest.json.asc": _object("manifest-signature"),
    }
    return PublicationPlan(
        immutable={
            transaction: _object("transaction", IMMUTABLE),
            "pool/main/d/dkc-linux/package.deb": _object("package", IMMUTABLE),
        },
        mutable_before_commit=precommit,
        inrelease=_object("inrelease"),
        state_pointer=_object("state"),
        conveniences=conveniences,
        mutable_preconditions={
            "dists/trixie/Release": None,
            "dists/trixie/InRelease": None,
            "state/current.asc": None,
            "manifest.json": None,
            "manifest.json.asc": None,
        },
        transaction_key=transaction,
    )


def test_conditional_store_has_no_unconditional_write() -> None:
    store = ConditionalObjectStore()
    with pytest.raises(ValueError, match="exactly one"):
        store.put("pool/x", b"x", IMMUTABLE)


def test_immutable_reuse_requires_equal_bytes_and_metadata() -> None:
    store = ConditionalObjectStore()
    first = store.create_immutable("pool/x", b"x", IMMUTABLE)
    assert store.create_immutable("pool/x", b"x", IMMUTABLE) == first
    with pytest.raises(PreconditionFailed, match="collision"):
        store.create_immutable("pool/x", b"different", IMMUTABLE)


def test_one_of_two_same_etag_updates_loses_with_412_semantics() -> None:
    store = ConditionalObjectStore()
    original = store.put("state/current.asc", b"old", MUTABLE, if_none_match=True)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def writer(body: bytes) -> None:
        barrier.wait()
        try:
            store.put("state/current.asc", body, MUTABLE, if_match=original.etag)
        except PreconditionFailed:
            outcomes.append("412")
        else:
            outcomes.append("2xx")

    threads = [
        threading.Thread(target=writer, args=(body,)) for body in (b"one", b"two")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["2xx", "412"]


def test_one_of_two_absent_path_creates_loses_with_412_semantics() -> None:
    store = ConditionalObjectStore()
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def writer(body: bytes) -> None:
        barrier.wait()
        try:
            store.put("manifest.json", body, MUTABLE, if_none_match=True)
        except PreconditionFailed:
            outcomes.append("412")
        else:
            outcomes.append("2xx")

    threads = [
        threading.Thread(target=writer, args=(body,)) for body in (b"one", b"two")
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["2xx", "412"]


@pytest.mark.parametrize("phase", range(1, 13))
def test_failure_after_every_phase_is_recovered_by_an_idempotent_retry(
    phase: int,
) -> None:
    store = ConditionalObjectStore()
    executor = PublicationExecutor(store)
    plan = _plan()
    with pytest.raises(InjectedFailure) as failure:
        executor.execute(plan, fail_after=phase)
    assert failure.value.phase == phase
    executor.execute(plan)


def test_repeated_completed_publication_is_an_exact_no_op() -> None:
    store = ConditionalObjectStore()
    plan = _plan()
    executor = PublicationExecutor(store)
    executor.execute(plan)
    keys = store.keys()
    before = {key: store.get(key) for key in keys}
    executor.execute(plan)
    assert store.keys() == keys
    assert {key: store.get(key) for key in keys} == before


def test_recovery_rejects_same_inrelease_bytes_with_wrong_metadata() -> None:
    store = ConditionalObjectStore()
    plan = _plan()
    executor = PublicationExecutor(store)
    with pytest.raises(InjectedFailure):
        executor.execute(plan, fail_after=8)
    current = store.get(plan.inrelease_key)
    assert current is not None
    store.put(
        plan.inrelease_key,
        current.body,
        IMMUTABLE,
        if_match=current.etag,
    )
    with pytest.raises(PreconditionFailed):
        executor.execute(plan)


def test_convenience_signatures_are_published_before_payloads() -> None:
    class RecordingStore(ConditionalObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.writes: list[str] = []

        def put(self, key: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            result = super().put(key, *args, **kwargs)  # type: ignore[arg-type]
            self.writes.append(key)
            return result

    store = RecordingStore()
    PublicationExecutor(store).execute(_plan())
    assert store.writes.index("manifest.json.asc") < store.writes.index("manifest.json")
    assert store.writes.index("manifest.json") < store.writes.index("state/current.asc")


def test_retained_objects_are_read_once_without_put_and_revalidated_by_head() -> None:
    class RecordingStore(ConditionalObjectStore):
        def __init__(self) -> None:
            super().__init__()
            self.reads: list[str] = []
            self.writes: list[str] = []
            self.heads: list[str] = []

        def get(self, key: str):  # type: ignore[no-untyped-def]
            self.reads.append(key)
            return super().get(key)

        def put(self, key: str, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
            self.writes.append(key)
            return super().put(key, *args, **kwargs)  # type: ignore[arg-type]

        def head(self, key: str, *, if_match: str):  # type: ignore[no-untyped-def]
            self.heads.append(key)
            return super().head(key, if_match=if_match)

    plan = _plan()
    store = RecordingStore()
    retained = "pool/main/d/dkc-linux/package.deb"
    value = plan.immutable[retained]
    store.create_immutable(retained, value.body, value.metadata)
    store.writes.clear()
    result = PublicationExecutor(store).execute(plan)
    assert retained not in store.writes
    assert store.reads.count(retained) == 1
    assert store.heads == [retained]
    assert store.writes.count(plan.transaction_key) == 1
    assert store.reads.count(plan.transaction_key) == 2  # absent probe + full read-back
    assert result.reused_objects == 1
    assert result.reused_bytes == len(value.body)
    assert result.created_objects == len(plan.immutable) - 1


@pytest.mark.parametrize("wrong_metadata", [False, True])
def test_retained_collision_fails_before_the_client_commit(
    wrong_metadata: bool,
) -> None:
    store = ConditionalObjectStore()
    plan = _plan()
    key = "pool/main/d/dkc-linux/package.deb"
    value = plan.immutable[key]
    store.put(
        key,
        value.body if wrong_metadata else b"corrupt",
        MUTABLE if wrong_metadata else value.metadata,
        if_none_match=True,
    )
    with pytest.raises(RuntimeError, match="immutable object collision"):
        PublicationExecutor(store).execute(plan)
    assert store.get(plan.inrelease_key) is None
    assert store.get(plan.state_key) is None


@pytest.mark.parametrize("change", ["bytes", "metadata", "size", "missing", "etag"])
def test_reused_objects_must_still_match_at_the_post_commit_boundary(
    change: str,
) -> None:
    store = ConditionalObjectStore()
    plan = _plan()
    key = "pool/main/d/dkc-linux/package.deb"
    value = plan.immutable[key]
    store.create_immutable(key, value.body, value.metadata)

    def progress(event):  # type: ignore[no-untyped-def]
        if event.phase != StoragePhase.VERIFY_REPOSITORY or event.objects:
            return
        current = store.get(key)
        assert current is not None
        if change == "missing":
            store.delete(key)
        else:
            # Retain the validator for size/metadata to prove neither field
            # is inferred from the ETag. Real S3 ETags need not cover metadata.
            body = b"corrupt" if change == "bytes" else value.body
            etag = '"changed"' if change in ("bytes", "etag") else current.etag
            store._objects[key] = replace(
                current,
                body=body + (b"x" if change == "size" else b""),
                metadata=MUTABLE if change == "metadata" else value.metadata,
                etag=etag,
            )

    with pytest.raises((PreconditionFailed, RuntimeError)):
        PublicationExecutor(store).execute(plan, progress=progress)
    assert store.get(plan.state_key) is None


def test_new_object_read_back_detects_a_corrupt_upload() -> None:
    class CorruptingStore(ConditionalObjectStore):
        def create_immutable(self, key, body, metadata):  # type: ignore[no-untyped-def]
            return super().create_immutable(key, body + b"corrupt", metadata)

    store = CorruptingStore()
    plan = _plan()
    with pytest.raises(RuntimeError, match="origin verification"):
        PublicationExecutor(store).execute(plan)
    assert store.get(plan.state_key) is None
