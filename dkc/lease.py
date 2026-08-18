"""Conditionally fenced production lease for publication and garbage collection."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .records import LeaseOwner, LeaseRecord
from .serialize import canonical_bytes, loads
from .storage import ObjectMetadata

__all__ = [
    "LEASE_KEY",
    "LeaseBusy",
    "LeaseHandle",
    "LeaseLost",
    "LeaseManager",
    "parse_lease",
]


LEASE_KEY = "state/locks/production.json"
LEASE_METADATA = ObjectMetadata(
    content_type="application/json",
    cache_control="private, no-store",
)


class LeaseBusy(RuntimeError):
    """Another live or not-provably-terminal holder owns the lease."""


class LeaseLost(RuntimeError):
    """The caller no longer owns the exact observed lease revision."""


class LeaseValue(Protocol):
    @property
    def body(self) -> bytes: ...

    @property
    def metadata(self) -> ObjectMetadata: ...

    @property
    def etag(self) -> str: ...


class LeaseStore(Protocol):
    def get(self, key: str) -> LeaseValue | None: ...

    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> LeaseValue: ...


TerminalProof = Callable[[LeaseOwner], bool]


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("lease clock must return an aware UTC datetime")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


def parse_lease(body: bytes) -> LeaseRecord:
    """Parse only the exact lease schema; unknown fields fail closed."""
    try:
        value = loads(body)
        if not isinstance(value, dict):
            raise ValueError("lease payload is not an object")
        allowed = {
            "schema",
            "status",
            "updated_utc",
            "owner",
            "acquired_utc",
            "expires_utc",
            "released_utc",
        }
        if set(value) - allowed:
            raise ValueError("lease payload has unknown fields")
        if value.get("schema") != "dkc.production-lease.v1":
            raise ValueError("lease payload has an unsupported schema")
        owner_value = value.get("owner")
        owner = None
        if owner_value is not None:
            if not isinstance(owner_value, dict):
                raise ValueError("lease owner is not an object")
            owner_fields = {
                "repository",
                "workflow_run_id",
                "run_attempt",
                "operation",
                "nonce",
            }
            if set(owner_value) != owner_fields or not all(
                isinstance(owner_value[field], str) for field in owner_fields
            ):
                raise ValueError("lease owner has an invalid field set")
            owner = LeaseOwner(**owner_value)
        record = LeaseRecord(
            status=value["status"],
            updated_utc=value["updated_utc"],
            owner=owner,
            acquired_utc=value.get("acquired_utc"),
            expires_utc=value.get("expires_utc"),
            released_utc=value.get("released_utc"),
            schema=value["schema"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LeaseLost("authoritative lease payload is invalid") from exc
    return record


@dataclass(frozen=True)
class LeaseHandle:
    owner: LeaseOwner
    record: LeaseRecord
    etag: str
    took_over_stale_holder: bool = False


class LeaseManager:
    """Acquire, renew, validate, and release one exact conditional lease."""

    def __init__(
        self,
        store: LeaseStore,
        *,
        ttl_seconds: int,
        takeover_grace_seconds: int,
        terminal_proof: TerminalProof,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds < 60:
            raise ValueError("lease TTL must be at least 60 seconds")
        if takeover_grace_seconds < 0:
            raise ValueError("lease takeover grace must not be negative")
        self.store = store
        self.ttl_seconds = ttl_seconds
        self.takeover_grace_seconds = takeover_grace_seconds
        self.terminal_proof = terminal_proof
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self.clock()
        _utc(value)
        return value.replace(microsecond=0)

    def _held_record(
        self, owner: LeaseOwner, now: datetime, *, acquired: datetime | None = None
    ) -> LeaseRecord:
        return LeaseRecord(
            status="held",
            updated_utc=_utc(now),
            owner=owner,
            acquired_utc=_utc(acquired or now),
            expires_utc=_utc(now + timedelta(seconds=self.ttl_seconds)),
        )

    @staticmethod
    def _require_exact_object(value: LeaseValue) -> LeaseRecord:
        if value.metadata != LEASE_METADATA:
            raise LeaseLost("authoritative lease metadata is invalid")
        return parse_lease(value.body)

    def acquire(self, owner: LeaseOwner) -> LeaseHandle:
        now = self._now()
        current = self.store.get(LEASE_KEY)
        if current is None:
            record = self._held_record(owner, now)
            created = self.store.put(
                LEASE_KEY,
                canonical_bytes(record.to_dict()),
                LEASE_METADATA,
                if_none_match=True,
            )
            return LeaseHandle(owner, record, created.etag)

        observed = self._require_exact_object(current)
        if observed.status == "held" and observed.owner == owner:
            if observed.expires_utc is None or now >= _parse_utc(observed.expires_utc):
                raise LeaseLost("the caller's previously held lease has expired")
            return LeaseHandle(owner, observed, current.etag)

        takeover = False
        if observed.status == "held":
            assert observed.owner is not None
            allowed = observed.stale_takeover_allowed(
                now_utc=_utc(now),
                grace_seconds=self.takeover_grace_seconds,
                old_run_terminal=self.terminal_proof(observed.owner),
            )
            if not allowed:
                raise LeaseBusy("production lease is held and cannot be safely taken over")
            takeover = True

        record = self._held_record(owner, now)
        replaced = self.store.put(
            LEASE_KEY,
            canonical_bytes(record.to_dict()),
            LEASE_METADATA,
            if_match=current.etag,
        )
        return LeaseHandle(owner, record, replaced.etag, takeover)

    def assert_batch_window(
        self,
        handle: LeaseHandle,
        *,
        batch_timeout_seconds: int,
        safety_seconds: int,
    ) -> None:
        if batch_timeout_seconds < 1 or safety_seconds < 0:
            raise ValueError("lease batch bounds are invalid")
        current = self.store.get(LEASE_KEY)
        if current is None or current.etag != handle.etag:
            raise LeaseLost("lease revision changed before a mutation batch")
        record = self._require_exact_object(current)
        if record.status != "held" or record.owner != handle.owner:
            raise LeaseLost("lease owner changed before a mutation batch")
        assert record.expires_utc is not None
        remaining = (_parse_utc(record.expires_utc) - self._now()).total_seconds()
        if remaining <= batch_timeout_seconds + safety_seconds:
            raise LeaseLost("lease has insufficient lifetime for the mutation batch")

    def renew(self, handle: LeaseHandle) -> LeaseHandle:
        now = self._now()
        current = self.store.get(LEASE_KEY)
        if current is None or current.etag != handle.etag:
            raise LeaseLost("lease revision changed before renewal")
        observed = self._require_exact_object(current)
        if observed.status != "held" or observed.owner != handle.owner:
            raise LeaseLost("lease owner changed before renewal")
        assert observed.expires_utc is not None
        if now >= _parse_utc(observed.expires_utc):
            raise LeaseLost("an expired lease cannot be renewed")
        assert observed.acquired_utc is not None
        renewed = self._held_record(
            handle.owner, now, acquired=_parse_utc(observed.acquired_utc)
        )
        written = self.store.put(
            LEASE_KEY,
            canonical_bytes(renewed.to_dict()),
            LEASE_METADATA,
            if_match=handle.etag,
        )
        return LeaseHandle(
            handle.owner, renewed, written.etag, handle.took_over_stale_holder
        )

    def release(self, handle: LeaseHandle) -> LeaseHandle:
        now = self._now()
        current = self.store.get(LEASE_KEY)
        if current is None or current.etag != handle.etag:
            raise LeaseLost("lease revision changed before release")
        observed = self._require_exact_object(current)
        if observed.status != "held" or observed.owner != handle.owner:
            raise LeaseLost("only the current exact holder may release the lease")
        released = LeaseRecord(
            status="free",
            updated_utc=_utc(now),
            released_utc=_utc(now),
        )
        written = self.store.put(
            LEASE_KEY,
            canonical_bytes(released.to_dict()),
            LEASE_METADATA,
            if_match=handle.etag,
        )
        return LeaseHandle(handle.owner, released, written.etag)
