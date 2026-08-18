from __future__ import annotations

import threading

import pytest

from dkc.publication import (
    InjectedFailure,
    PublicationExecutor,
    PublicationObject,
    PublicationPlan,
)
from dkc.storage import ObjectMetadata
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

    threads = [threading.Thread(target=writer, args=(body,)) for body in (b"one", b"two")]
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

    threads = [threading.Thread(target=writer, args=(body,)) for body in (b"one", b"two")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["2xx", "412"]


@pytest.mark.parametrize("phase", range(1, 13))
def test_failure_after_every_phase_is_recovered_by_an_idempotent_retry(phase: int) -> None:
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
    before = {key: store.get(key) for key in store.keys()}
    executor.execute(plan)
    assert {key: store.get(key) for key in store.keys()} == before


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
