from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dkc.lease import LeaseManager
from dkc.publication import InjectedFailure, PublicationExecutor, PublicationObject
from dkc.records import LeaseOwner
from dkc.remote_store import S3ObjectStore
from dkc.s3 import PreconditionFailed
from dkc.storage_progress import StoragePhase
from tests.http_storage import HttpStorage, certificate
from tests.test_publication import IMMUTABLE, MUTABLE, _plan


@pytest.fixture(scope="module")
def tls_certificate(tmp_path_factory: pytest.TempPathFactory):  # type: ignore[no-untyped-def]
    return certificate(tmp_path_factory.mktemp("storage-tls"))


@pytest.fixture
def storage(tls_certificate: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    cert, key = tls_certificate
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    fixture = HttpStorage(cert, key)
    try:
        yield fixture
    finally:
        fixture.close()


def test_https_publication_reuses_payload_once_without_upload(storage: HttpStorage) -> None:
    initial = _plan()
    payloads = {
        f"pool/main/d/dkc-linux/large-{number}.deb": PublicationObject(
            bytes([number]) * (256 * 1024), IMMUTABLE
        )
        for number in range(4)
    }
    plan = replace(initial, immutable={**initial.immutable, **payloads})
    store = S3ObjectStore(storage.client)
    manager = LeaseManager(store, ttl_seconds=900, takeover_grace_seconds=0, terminal_proof=lambda _: False)
    lease = manager.acquire(LeaseOwner("owner/repo", "1", "1", "publish", "a" * 32))

    def checkpoint() -> None:
        nonlocal lease
        manager.assert_batch_window(lease, batch_timeout_seconds=120, safety_seconds=60)
        lease = manager.renew(lease)

    executor = PublicationExecutor(store)
    executor.execute(plan, mutation_checkpoint=checkpoint)
    storage.reset_counters()
    result = executor.execute(plan, mutation_checkpoint=checkpoint)
    assert result.created_objects == 0
    assert result.reused_objects == len(plan.immutable)
    for key, value in payloads.items():
        assert storage.requests["GET", key] == 1
        assert storage.requests["HEAD", key] == 1
        assert storage.requests["PUT", key] == 0
        assert storage.received_bytes[key] == 0
        assert storage.returned_bytes[key] == len(value.body)
    manager.release(lease)


@pytest.mark.parametrize("phase", range(1, 13))
def test_https_retry_recovers_every_publication_boundary(storage: HttpStorage, phase: int) -> None:
    store = S3ObjectStore(storage.client)
    executor = PublicationExecutor(store)
    plan = _plan()
    with pytest.raises(InjectedFailure):
        executor.execute(plan, fail_after=phase)
    executor.execute(plan)
    assert store.get(plan.state_key).body == plan.state_pointer.body  # type: ignore[union-attr]


def test_https_lost_put_reply_is_reconciled_without_a_second_upload(storage: HttpStorage) -> None:
    plan = _plan()
    pool_key = "pool/main/d/dkc-linux/package.deb"
    storage.drop_reply_for = pool_key
    PublicationExecutor(S3ObjectStore(storage.client)).execute(plan)
    assert storage.requests["PUT", pool_key] == 1
    assert storage.requests["GET", pool_key] == 3  # absent probe, reconciliation, read-back


def test_https_transient_head_failure_retries_without_downloading_again(storage: HttpStorage) -> None:
    plan = _plan()
    pool_key = "pool/main/d/dkc-linux/package.deb"
    executor = PublicationExecutor(S3ObjectStore(storage.client))
    executor.execute(plan)
    storage.reset_counters()
    storage.transient_head_for = pool_key
    executor.execute(plan)
    assert storage.requests["HEAD", pool_key] == 2
    assert storage.requests["GET", pool_key] == 1
    assert storage.received_bytes[pool_key] == 0


@pytest.mark.parametrize("change", ["bytes", "metadata", "missing"])
def test_https_changed_reused_object_cannot_commit_controller_state(storage: HttpStorage, change: str) -> None:
    plan = _plan()
    pool_key = "pool/main/d/dkc-linux/package.deb"
    value = plan.immutable[pool_key]
    storage.store.create_immutable(pool_key, value.body, value.metadata)

    def progress(event):  # type: ignore[no-untyped-def]
        if event.phase == StoragePhase.VERIFY_REPOSITORY and event.objects == 0:
            current = storage.store.get(pool_key)
            assert current is not None
            if change == "missing":
                storage.store.delete(pool_key)
            else:
                storage.store._objects[pool_key] = replace(
                    current,
                    body=b"corrupt" if change == "bytes" else current.body,
                    metadata=MUTABLE if change == "metadata" else current.metadata,
                    etag='"changed"' if change == "bytes" else current.etag,
                )

    with pytest.raises(RuntimeError):  # includes 404 and 412 RemoteStoreError
        PublicationExecutor(S3ObjectStore(storage.client)).execute(plan, progress=progress)
    assert storage.store.get(plan.state_key) is None


def test_https_lost_mutable_precondition_is_not_reconciled_to_success(storage: HttpStorage) -> None:
    store = S3ObjectStore(storage.client)
    winner = store.put("state/current.asc", b"winner", MUTABLE, if_none_match=True)
    with pytest.raises(PreconditionFailed):
        store.put("state/current.asc", b"winner", MUTABLE, if_match='"stale"')
    assert store.get("state/current.asc") == winner
