"""Typed records for signed state, transactions, and garbage collection.

These are the objects a publication actually commits. They exist as types rather
than as dictionaries assembled at call sites because each one is signed, hashed,
and compared against an expectation: a field that is silently absent or subtly
differently shaped is not a cosmetic problem, it is a failed conditional write
or a wrong reachability computation.

Every record serializes through `dkc.serialize`, so two runs that mean the same
thing produce the same bytes, and validates against its schema in `schemas/`.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .serialize import sha256_of

__all__ = [
    "Artifact",
    "CacheClass",
    "Decision",
    "DiscoveryDecision",
    "GcPlan",
    "GcQueueEntry",
    "GcTarget",
    "LeaseOwner",
    "LeaseRecord",
    "PublicationManifest",
    "StatePointer",
    "TransactionRecord",
    "IMMUTABLE_GC_PREFIXES",
]

CacheClass = Literal["immutable", "mutable"]
Decision = Literal["no_op", "build", "maintenance", "blocked"]
LtoMode = Literal["none", "thin", "full"]

# Deletion may only ever touch these. Mutable metadata, keys, state pointers and
# locks are excluded by construction rather than by a check that could be
# forgotten at a call site.
IMMUTABLE_GC_PREFIXES: tuple[str, ...] = (
    "pool/",
    "state/publications/",
    "state/transactions/",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[0-9a-z-]{8,64}$")
_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")


def _utc(value: str, field_name: str) -> datetime:
    """Parse the one canonical timestamp form used by signed records."""
    if not _UTC_RE.fullmatch(value):
        raise ValueError(f"{field_name} must use canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a real UTC date: {value!r}") from exc
    return parsed


def _sha256(value: str, field_name: str) -> None:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


def _object_key(key: str, field_name: str = "object key") -> None:
    segments = key.split("/")
    if (
        not key
        or key != key.strip()
        or key.startswith("/")
        or key.endswith("/")
        or "*" in key
        or "\\" in key
        or _CONTROL_RE.search(key)
        or any(segment in ("", ".", "..") for segment in segments)
    ):
        raise ValueError(f"unsafe {field_name}: {key!r}")


def _gc_key(key: str) -> None:
    _object_key(key, "deletion key")
    by_hash = key.startswith("dists/") and "/by-hash/SHA256/" in key
    if not (by_hash or key.startswith(IMMUTABLE_GC_PREFIXES)):
        raise ValueError(f"deletion key is outside the immutable prefixes: {key!r}")


def _drop_none(value: Any) -> Any:
    """Remove keys whose value is None.

    An absent optional field and a field explicitly set to null would otherwise
    hash differently while meaning the same thing.
    """
    if isinstance(value, dict):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_drop_none(v) for v in value]
    return value


@dataclass(frozen=True)
class _Record:
    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = _drop_none(asdict(self))
        return result

    def digest(self) -> str:
        """Named `digest`, not `sha256`: a base-class method called `sha256`
        becomes the inherited default for a subclass field of the same name,
        and dataclasses then reject every non-default field after it."""
        return sha256_of(self.to_dict())


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryDecision(_Record):
    """The typed output of the discovery graph."""

    decision: Decision
    source_version: str
    source_dsc_sha256: str
    dkc_revision: int
    build_policy_sha256: str
    lto_mode: LtoMode
    utc: str
    retention_mode: Literal["series", "series-size"] = "series-size"
    retention_max_bytes: int | None = 9_500_000_000
    build_required: bool = False
    maintenance_required: bool = False
    publish_allowed: bool = False
    authoritative_state_read: bool = False
    state_generation: int | None = None
    state_publication_id: str | None = None
    reason: str | None = None
    schema: str = "dkc.discovery-decision.v1"

    def __post_init__(self) -> None:
        _sha256(self.source_dsc_sha256, "source_dsc_sha256")
        _sha256(self.build_policy_sha256, "build_policy_sha256")
        if self.dkc_revision < 1:
            raise ValueError("dkc_revision must be positive")
        if self.lto_mode not in ("none", "thin", "full"):
            raise ValueError("lto_mode must be none, thin, or full")
        if self.retention_mode == "series":
            if self.retention_max_bytes is not None:
                raise ValueError("series retention must not carry a byte limit")
        elif self.retention_mode == "series-size":
            if self.retention_max_bytes is None or self.retention_max_bytes < 1:
                raise ValueError("series-size retention requires a positive byte limit")
        else:
            raise ValueError("unknown retention mode")
        _utc(self.utc, "utc")
        if self.state_generation is not None and self.state_generation < 0:
            raise ValueError("state_generation must not be negative")
        if self.state_publication_id is not None and not _ID_RE.fullmatch(
            self.state_publication_id
        ):
            raise ValueError("state_publication_id is unsafe")
        if (self.state_generation is None) != (self.state_publication_id is None):
            raise ValueError(
                "state generation and publication identity must travel together"
            )
        # A public, replayable copy of state can trigger conservative extra work
        # but can never authorize a mutation.
        if self.publish_allowed and not self.authoritative_state_read:
            raise ValueError(
                "publish_allowed requires an authoritative state read; "
                "a public hint cannot authorize publication"
            )
        if self.decision == "blocked" and (self.build_required or self.publish_allowed):
            raise ValueError("a blocked decision cannot also require work or allow publishing")
        expected_flags = {
            "no_op": (False, False, False),
            "build": (True, False, True),
            "maintenance": (False, True, True),
            "blocked": (False, False, False),
        }[self.decision]
        observed_flags = (
            self.build_required,
            self.maintenance_required,
            self.publish_allowed,
        )
        if observed_flags != expected_flags:
            raise ValueError(
                f"{self.decision} decision has a contradictory work/permission state"
            )
        if self.decision in ("no_op", "build", "maintenance") and not (
            self.authoritative_state_read
        ):
            raise ValueError(
                f"{self.decision} decision requires an authoritative state read"
            )
        if self.decision in ("no_op", "maintenance") and self.state_generation is None:
            raise ValueError(
                f"{self.decision} decision requires a published state generation"
            )


# --------------------------------------------------------------------------
# Publication
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Artifact(_Record):
    key: str
    sha256: str
    size: int
    media_type: str
    cache_class: CacheClass
    provenance_ref: str | None = None

    def __post_init__(self) -> None:
        _object_key(self.key)
        _sha256(self.sha256, "artifact sha256")
        if self.size < 0:
            raise ValueError("artifact size must not be negative")
        if not self.media_type or _CONTROL_RE.search(self.media_type):
            raise ValueError("artifact media_type is empty or unsafe")
        if self.cache_class not in ("immutable", "mutable"):
            raise ValueError("artifact cache_class must be immutable or mutable")
        if self.provenance_ref is not None:
            _object_key(self.provenance_ref, "provenance reference")


@dataclass(frozen=True)
class GcQueueEntry(_Record):
    key: str
    sha256: str
    size: int
    reason: str
    tombstoned: bool = True

    def __post_init__(self) -> None:
        _gc_key(self.key)
        _sha256(self.sha256, "GC object sha256")
        if self.size < 0:
            raise ValueError("GC object size must not be negative")
        if not self.reason or _CONTROL_RE.search(self.reason):
            raise ValueError("a GC queue entry needs a safe non-empty reason")
        if not self.tombstoned:
            raise ValueError(
                "a queue entry is a permanent tombstone; a key that could become "
                "live again must never enter the queue"
            )


@dataclass(frozen=True)
class StatePointer(_Record):
    generation: int
    publication_id: str
    manifest_key: str
    manifest_sha256: str
    committed_utc: str
    previous_generation: int | None = None
    schema: str = "dkc.state-pointer.v1"

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("generation must not be negative")
        if not _ID_RE.fullmatch(self.publication_id):
            raise ValueError(f"unsafe publication id: {self.publication_id!r}")
        _sha256(self.manifest_sha256, "manifest_sha256")
        _utc(self.committed_utc, "committed_utc")
        expected = f"state/publications/{self.publication_id}/manifest.json"
        if self.manifest_key != expected:
            raise ValueError(f"manifest key {self.manifest_key!r} does not match the publication id")
        if self.generation == 0:
            if self.previous_generation is not None:
                raise ValueError("bootstrap generation cannot name a predecessor")
        elif self.previous_generation != self.generation - 1:
            raise ValueError("generation must increase by exactly one")


@dataclass(frozen=True)
class PublicationManifest(_Record):
    generation: int
    publication_id: str
    transaction_id: str
    source_version: str
    source_dsc_sha256: str
    dkc_version: str
    dkc_revision: int
    build_policy_sha256: str
    lto_mode: LtoMode
    build_id: str
    retained_series: list[list[int]]
    artifacts: list[Artifact]
    live_objects: list[str]
    apt_metadata: dict[str, Any]
    meta_packages: dict[str, str]
    created_utc: str
    retention_mode: Literal["series", "series-size"] = "series"
    retention_max_bytes: int | None = None
    generation_snapshot_prefix: str | None = None
    provenance_ref: str | None = None
    gc_queue: list[GcQueueEntry] = field(default_factory=list)
    previous_publication: dict[str, Any] | None = None
    schema: str = "dkc.publication-manifest.v1"

    def __post_init__(self) -> None:
        if self.generation < 0 or self.dkc_revision < 1:
            raise ValueError("generation must be non-negative and DKC revision positive")
        for name, value in (("publication", self.publication_id), ("transaction", self.transaction_id)):
            if not _ID_RE.fullmatch(value):
                raise ValueError(f"unsafe {name} id: {value!r}")
        if not self.source_version or not self.dkc_version:
            raise ValueError("source and DKC versions must be non-empty")
        _sha256(self.source_dsc_sha256, "source_dsc_sha256")
        _sha256(self.build_policy_sha256, "build_policy_sha256")
        if self.lto_mode not in ("none", "thin", "full"):
            raise ValueError("lto_mode must be none, thin, or full")
        if not re.fullmatch(r"[0-9a-f]{12,64}", self.build_id):
            raise ValueError("build_id must be 12 to 64 lowercase hexadecimal digits")
        _utc(self.created_utc, "created_utc")
        if len(self.retained_series) > 3:
            raise ValueError("retention keeps at most the newest three upstream series")
        normalized_series = [tuple(series) for series in self.retained_series]
        if any(
            len(series) != 2 or any(not isinstance(part, int) or part < 0 for part in series)
            for series in self.retained_series
        ):
            raise ValueError("every retained series must be a non-negative [X, Y] pair")
        if len(normalized_series) != len(set(normalized_series)):
            raise ValueError("retained series must not contain duplicates")
        if normalized_series != sorted(normalized_series, reverse=True):
            raise ValueError("retained series must be ordered newest first")
        if self.retention_mode == "series":
            if self.retention_max_bytes is not None:
                raise ValueError("series retention must not carry a byte limit")
        elif self.retention_mode == "series-size":
            if self.retention_max_bytes is None or self.retention_max_bytes < 1:
                raise ValueError("series-size retention requires a positive byte limit")
        else:
            raise ValueError("unknown retention mode")

        artifact_keys = [artifact.key for artifact in self.artifacts]
        if len(artifact_keys) != len(set(artifact_keys)):
            raise ValueError("publication artifacts must have unique keys")
        keys = set(artifact_keys)
        if len(self.live_objects) != len(set(self.live_objects)):
            raise ValueError("live_objects must not contain duplicates")
        unknown = [k for k in self.live_objects if k not in keys]
        if unknown:
            raise ValueError(f"live objects not present among artifacts: {unknown}")

        # A tombstoned key must not also be advertised as live. This is the
        # invariant that stops a suspended GC process from deleting content a
        # later publication started serving.
        queued = {entry.key for entry in self.gc_queue}
        if len(queued) != len(self.gc_queue):
            raise ValueError("GC queue keys must be unique permanent tombstones")
        both = sorted(queued & set(self.live_objects))
        if both:
            raise ValueError(f"keys are both live and queued for deletion: {both}")

        if self.previous_publication is None:
            if self.generation != 0:
                raise ValueError("a non-bootstrap publication must identify its predecessor")
        else:
            if set(self.previous_publication) != {"publication_id", "generation"}:
                raise ValueError("previous_publication has an unexpected field set")
            previous_id = self.previous_publication["publication_id"]
            previous_generation = self.previous_publication["generation"]
            if not isinstance(previous_id, str) or not _ID_RE.fullmatch(previous_id):
                raise ValueError("previous_publication has an unsafe publication ID")
            if (
                not isinstance(previous_generation, int)
                or isinstance(previous_generation, bool)
                or previous_generation != self.generation - 1
            ):
                raise ValueError("previous_publication generation is not the predecessor")

# --------------------------------------------------------------------------
# Transactions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LeaseOwner(_Record):
    repository: str
    workflow_run_id: str
    run_attempt: str
    operation: str
    nonce: str

    def __post_init__(self) -> None:
        if not _REPOSITORY_RE.fullmatch(self.repository):
            raise ValueError("lease owner repository must be an owner/name pair")
        if (
            not self.workflow_run_id.isdecimal()
            or not self.run_attempt.isdecimal()
            or int(self.workflow_run_id) < 1
            or int(self.run_attempt) < 1
        ):
            raise ValueError("lease workflow identifiers must be decimal strings")
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,31}", self.operation):
            raise ValueError("lease operation is unsafe")
        if not re.fullmatch(r"[0-9a-f]{32,128}", self.nonce):
            raise ValueError("lease nonce must contain at least 128 random bits")


@dataclass(frozen=True)
class LeaseRecord(_Record):
    status: Literal["held", "free"]
    updated_utc: str
    owner: LeaseOwner | None = None
    acquired_utc: str | None = None
    expires_utc: str | None = None
    released_utc: str | None = None
    schema: str = "dkc.production-lease.v1"

    def __post_init__(self) -> None:
        if self.status not in ("held", "free"):
            raise ValueError("lease status must be held or free")
        updated = _utc(self.updated_utc, "updated_utc")
        if self.status == "held":
            if self.owner is None or self.acquired_utc is None or self.expires_utc is None:
                raise ValueError("a held lease requires owner, acquisition, and expiry")
            if not isinstance(self.owner, LeaseOwner):
                raise ValueError("a held lease owner must be a validated LeaseOwner")
            if self.released_utc is not None:
                raise ValueError("a held lease cannot have a release timestamp")
            acquired = _utc(self.acquired_utc, "acquired_utc")
            expires = _utc(self.expires_utc, "expires_utc")
            if not acquired <= updated < expires:
                raise ValueError("held lease timestamps are not monotonic")
        else:
            if self.owner is not None or self.acquired_utc is not None or self.expires_utc is not None:
                raise ValueError("a free lease cannot retain active ownership")
            if self.released_utc is None or _utc(self.released_utc, "released_utc") != updated:
                raise ValueError("a free lease requires its exact release timestamp")

    def stale_takeover_allowed(
        self, *, now_utc: str, grace_seconds: int, old_run_terminal: bool
    ) -> bool:
        if grace_seconds < 0:
            raise ValueError("lease takeover grace must not be negative")
        if self.status != "held" or self.expires_utc is None or not old_run_terminal:
            return False
        now = _utc(now_utc, "now_utc")
        expires = _utc(self.expires_utc, "expires_utc")
        return (now - expires).total_seconds() >= grace_seconds

@dataclass(frozen=True)
class TransactionRecord(_Record):
    transaction_id: str
    publication_id: str
    expected_generation: int
    intended_inrelease_sha256: str
    started_utc: str
    phases: list[dict[str, Any]] = field(default_factory=list)
    owner: dict[str, str] | None = None
    mutable_preconditions: list[dict[str, Any]] = field(default_factory=list)
    schema: str = "dkc.transaction.v1"

    def __post_init__(self) -> None:
        for name, value in (("transaction", self.transaction_id), ("publication", self.publication_id)):
            if not _ID_RE.fullmatch(value):
                raise ValueError(f"unsafe {name} id: {value!r}")
        if self.expected_generation < 0:
            raise ValueError("expected_generation must not be negative")
        _sha256(self.intended_inrelease_sha256, "intended_inrelease_sha256")
        _utc(self.started_utc, "started_utc")

        if self.owner is not None:
            expected_owner_fields = {"repository", "run_id", "run_attempt", "nonce"}
            if set(self.owner) != expected_owner_fields:
                raise ValueError("transaction owner has an unexpected field set")
            if not _REPOSITORY_RE.fullmatch(self.owner["repository"]):
                raise ValueError("transaction owner repository must be an owner/name pair")
            if (
                not self.owner["run_id"].isdecimal()
                or not self.owner["run_attempt"].isdecimal()
                or int(self.owner["run_id"]) < 1
                or int(self.owner["run_attempt"]) < 1
            ):
                raise ValueError("transaction owner run identifiers must be decimal strings")
            if not re.fullmatch(r"[0-9a-f]{32,128}", self.owner["nonce"]):
                raise ValueError("transaction owner nonce must contain at least 128 random bits")

        phase_numbers: list[int] = []
        for phase in self.phases:
            number = phase.get("phase")
            state = phase.get("state")
            if not isinstance(number, int) or not 1 <= number <= 14:
                raise ValueError(f"invalid transaction phase number: {number!r}")
            if state not in ("pending", "committed", "failed"):
                raise ValueError(f"invalid transaction phase state: {state!r}")
            if "utc" in phase:
                if not isinstance(phase["utc"], str):
                    raise ValueError("transaction phase utc must be a string")
                _utc(phase["utc"], "phase utc")
            phase_numbers.append(number)
        if len(phase_numbers) != len(set(phase_numbers)):
            raise ValueError("transaction phases must not contain duplicates")

        precondition_keys: list[str] = []
        for precondition in self.mutable_preconditions:
            key = precondition.get("key")
            if not isinstance(key, str):
                raise ValueError("mutable precondition key must be a string")
            _object_key(key, "mutable precondition key")
            kind = precondition.get("precondition")
            etag = precondition.get("etag")
            if kind == "if-none-match" and etag is not None:
                raise ValueError("if-none-match precondition must not carry an ETag")
            if kind == "if-match" and (not isinstance(etag, str) or not etag):
                raise ValueError("if-match precondition requires a non-empty ETag")
            if kind not in ("if-none-match", "if-match"):
                raise ValueError(f"invalid mutable precondition: {kind!r}")
            precondition_keys.append(key)
        if len(precondition_keys) != len(set(precondition_keys)):
            raise ValueError("mutable preconditions must have unique keys")

# --------------------------------------------------------------------------
# Garbage collection
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class GcTarget(_Record):
    key: str
    sha256: str
    reason: str
    size: int

    def __post_init__(self) -> None:
        _gc_key(self.key)
        _sha256(self.sha256, "deletion target sha256")
        if self.size < 0:
            raise ValueError("deletion size must not be negative")
        if not self.reason or _CONTROL_RE.search(self.reason):
            raise ValueError("a deletion target needs a safe non-empty reason")


@dataclass(frozen=True)
class GcPlan(_Record):
    expected_generation: int
    targets: list[GcTarget]
    caps: dict[str, int]
    planned_utc: str
    schema: str = "dkc.gc-plan.v1"

    def __post_init__(self) -> None:
        if self.expected_generation < 0:
            raise ValueError("expected_generation must not be negative")
        _utc(self.planned_utc, "planned_utc")
        for required in ("max_objects", "max_bytes"):
            if required not in self.caps or self.caps[required] <= 0:
                raise ValueError(f"caps.{required} must be a positive integer")
        if len(self.targets) > self.caps["max_objects"]:
            raise ValueError(
                f"plan has {len(self.targets)} targets, over the cap of {self.caps['max_objects']}"
            )
        total = sum(t.size for t in self.targets)
        if total > self.caps["max_bytes"]:
            raise ValueError(f"plan totals {total} bytes, over the cap of {self.caps['max_bytes']}")
        keys = [t.key for t in self.targets]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate keys in a deletion plan")
