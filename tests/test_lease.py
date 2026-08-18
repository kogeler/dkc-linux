from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dkc.lease import LEASE_KEY, LeaseBusy, LeaseLost, LeaseManager
from dkc.records import LeaseOwner, LeaseRecord
from dkc.serialize import canonical_bytes
from dkc.storage import ObjectMetadata
from tests.fake_storage import ConditionalObjectStore, PreconditionFailed


UTC = timezone.utc
MUTABLE = ObjectMetadata("application/json", "private, no-store")


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def owner(nonce: str = "a" * 32, run: str = "1") -> LeaseOwner:
    return LeaseOwner("kogeler/dkc-linux", run, "1", "publish", nonce)


def manager(
    store: ConditionalObjectStore,
    clock: Clock,
    *,
    terminal: bool = False,
) -> LeaseManager:
    return LeaseManager(
        store,
        ttl_seconds=300,
        takeover_grace_seconds=60,
        terminal_proof=lambda _owner: terminal,
        clock=clock,
    )


def test_bootstrap_acquire_renew_batch_check_and_release() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    lease = manager(store, clock)
    first = lease.acquire(owner())
    lease.assert_batch_window(first, batch_timeout_seconds=120, safety_seconds=60)
    clock.advance(30)
    renewed = lease.renew(first)
    assert renewed.etag != first.etag
    released = lease.release(renewed)
    assert released.record.status == "free"
    assert lease.acquire(owner("b" * 32, "2")).record.status == "held"


def test_competing_acquire_loses_without_modifying_the_lease() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    lease = manager(store, clock)
    first = lease.acquire(owner())
    with pytest.raises(LeaseBusy):
        lease.acquire(owner("b" * 32, "2"))
    assert store.get(LEASE_KEY).etag == first.etag  # type: ignore[union-attr]


def test_expiry_alone_never_authorizes_takeover() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    first = manager(store, clock).acquire(owner())
    clock.advance(1000)
    with pytest.raises(LeaseBusy):
        manager(store, clock, terminal=False).acquire(owner("b" * 32, "2"))
    assert store.get(LEASE_KEY).etag == first.etag  # type: ignore[union-attr]


def test_terminal_proof_and_grace_are_both_required_for_takeover() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    old = manager(store, clock).acquire(owner())
    clock.advance(301)
    with pytest.raises(LeaseBusy):
        manager(store, clock, terminal=True).acquire(owner("b" * 32, "2"))
    clock.advance(60)
    taken = manager(store, clock, terminal=True).acquire(owner("b" * 32, "2"))
    assert taken.took_over_stale_holder
    with pytest.raises(LeaseLost):
        manager(store, clock).release(old)


def test_old_holder_cannot_renew_or_release_after_its_etag_changes() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    lease = manager(store, clock)
    old = lease.acquire(owner())
    current = lease.renew(old)
    with pytest.raises(LeaseLost):
        lease.renew(old)
    with pytest.raises(LeaseLost):
        lease.release(old)
    lease.release(current)


def test_expired_holder_cannot_resume_by_renewing() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    lease = manager(store, clock)
    handle = lease.acquire(owner())
    clock.advance(300)
    with pytest.raises(LeaseLost, match="expired"):
        lease.renew(handle)


def test_batch_window_fails_closed_near_expiry() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    lease = manager(store, clock)
    handle = lease.acquire(owner())
    clock.advance(121)
    with pytest.raises(LeaseLost, match="insufficient"):
        lease.assert_batch_window(
            handle, batch_timeout_seconds=120, safety_seconds=60
        )


def test_conditional_renewal_loss_is_not_retried_unconditionally() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    lease = manager(store, clock)
    handle = lease.acquire(owner())
    replacement = LeaseRecord(
        status="free",
        updated_utc="2026-08-17T12:00:01Z",
        released_utc="2026-08-17T12:00:01Z",
    )
    store.put(
        LEASE_KEY,
        canonical_bytes(replacement.to_dict()),
        MUTABLE,
        if_match=handle.etag,
    )
    with pytest.raises(LeaseLost):
        lease.renew(handle)


def test_lease_parser_rejects_unknown_or_wrong_metadata() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    bad = store.put(
        LEASE_KEY,
        b'{"schema":"dkc.production-lease.v1","status":"free",'
        b'"updated_utc":"2026-08-17T12:00:00Z",'
        b'"released_utc":"2026-08-17T12:00:00Z","extra":true}\n',
        MUTABLE,
        if_none_match=True,
    )
    assert bad is not None
    with pytest.raises(LeaseLost, match="payload"):
        manager(store, clock).acquire(owner())


def test_concurrent_conditional_acquires_have_exactly_one_winner() -> None:
    store = ConditionalObjectStore()
    clock = Clock()
    first = manager(store, clock)
    second = manager(store, clock)
    observed = store.get(LEASE_KEY)
    assert observed is None
    a = first._held_record(owner(), clock())
    b = second._held_record(owner("b" * 32, "2"), clock())
    store.put(LEASE_KEY, canonical_bytes(a.to_dict()), MUTABLE, if_none_match=True)
    with pytest.raises(PreconditionFailed):
        store.put(LEASE_KEY, canonical_bytes(b.to_dict()), MUTABLE, if_none_match=True)
