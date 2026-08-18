"""Signed-state records: schema conformance and the invariants behind them."""

from __future__ import annotations

import pytest

from dkc import schema
from dkc.records import (
    Artifact,
    DiscoveryDecision,
    GcPlan,
    GcQueueEntry,
    GcTarget,
    LeaseOwner,
    LeaseRecord,
    PublicationManifest,
    StatePointer,
    TransactionRecord,
)
from dkc.serialize import dumps

PUB = "20260811-abc12345"
TXN = "20260811-def67890"
SHA = "a" * 64
NOW = "2026-08-11T00:00:00Z"


def _artifact(key: str = "pool/main/d/dkc-linux/x.deb", cache: str = "immutable") -> Artifact:
    return Artifact(key=key, sha256=SHA, size=1, media_type="application/vnd.debian.binary-package",
                    cache_class=cache)  # type: ignore[arg-type]


def _manifest(**overrides: object) -> PublicationManifest:
    base = dict(
        generation=1,
        publication_id=PUB,
        transaction_id=TXN,
        source_version="7.1.7-1",
        source_dsc_sha256=SHA,
        dkc_version="7.1.7-1+dkc13.1",
        dkc_revision=1,
        build_policy_sha256="c" * 64,
        lto_mode="thin",
        build_id="b" * 12,
        retained_series=[[7, 1], [7, 0], [6, 18]],
        artifacts=[_artifact()],
        live_objects=["pool/main/d/dkc-linux/x.deb"],
        apt_metadata={
            "inrelease_sha256": SHA,
            "date": "Tue, 11 Aug 2026 00:00:00 UTC",
            "valid_until": "Tue, 25 Aug 2026 00:00:00 UTC",
            "index_hashes": {"dists/trixie/main/binary-amd64/Packages": SHA},
        },
        meta_packages={"dkc-linux-image-v3-amd64": "7.1.7-1+dkc13.1"},
        created_utc=NOW,
    )
    base.update(overrides)
    if "previous_publication" not in overrides:
        generation = int(base["generation"])
        base["previous_publication"] = (
            None
            if generation == 0
            else {
                "publication_id": "20260810-99999999",
                "generation": generation - 1,
            }
        )
    return PublicationManifest(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Every record must match its schema
# --------------------------------------------------------------------------


def test_all_record_schemas_exist() -> None:
    assert sorted(
        path.name.removesuffix(".schema.json")
        for path in schema.SCHEMA_DIR.glob("*.schema.json")
    ) == [
        "authoritative-state-read",
        "discovery-decision",
        "gc-plan",
        "pool-export",
        "production-lease",
        "provenance",
        "publication-manifest",
        "repository-signing-request",
        "source-inventory",
        "state-pointer",
        "transaction",
    ]


def test_lease_matches_schema() -> None:
    lease = LeaseRecord(
        status="held",
        updated_utc=NOW,
        acquired_utc=NOW,
        expires_utc="2026-08-11T01:00:00Z",
        owner=LeaseOwner(
            repository="kogeler/dkc-linux",
            workflow_run_id="1",
            run_attempt="1",
            operation="publish",
            nonce="b" * 32,
        ),
    )
    schema.validate("production-lease", lease.to_dict())


def test_expiry_alone_never_allows_stale_lease_takeover() -> None:
    lease = LeaseRecord(
        status="held",
        updated_utc=NOW,
        acquired_utc=NOW,
        expires_utc="2026-08-11T01:00:00Z",
        owner=LeaseOwner(
            repository="kogeler/dkc-linux",
            workflow_run_id="1",
            run_attempt="1",
            operation="publish",
            nonce="b" * 32,
        ),
    )
    assert not lease.stale_takeover_allowed(
        now_utc="2026-08-11T02:00:00Z",
        grace_seconds=60,
        old_run_terminal=False,
    )
    assert lease.stale_takeover_allowed(
        now_utc="2026-08-11T02:00:00Z",
        grace_seconds=60,
        old_run_terminal=True,
    )


def test_manifest_matches_schema() -> None:
    schema.validate("publication-manifest", _manifest().to_dict())


def test_state_pointer_matches_schema() -> None:
    pointer = StatePointer(
        generation=2,
        publication_id=PUB,
        manifest_key=f"state/publications/{PUB}/manifest.json",
        manifest_sha256=SHA,
        committed_utc=NOW,
        previous_generation=1,
    )
    schema.validate("state-pointer", pointer.to_dict())


def test_transaction_matches_schema() -> None:
    record = TransactionRecord(
        transaction_id=TXN,
        publication_id=PUB,
        expected_generation=1,
        intended_inrelease_sha256=SHA,
        started_utc=NOW,
        phases=[{"phase": 5, "name": "upload-transaction", "state": "committed", "utc": NOW}],
        owner={"repository": "kogeler/dkc-linux", "run_id": "1", "run_attempt": "1", "nonce": "b" * 32},
    )
    schema.validate("transaction", record.to_dict())


def test_decision_matches_schema() -> None:
    decision = DiscoveryDecision(
        decision="build", source_version="7.1.7-1", source_dsc_sha256=SHA, utc=NOW,
        dkc_revision=1, build_policy_sha256="c" * 64, lto_mode="thin",
        build_required=True, authoritative_state_read=True, publish_allowed=True,
        state_generation=3, state_publication_id="20260817-abcdef12",
    )
    schema.validate("discovery-decision", decision.to_dict())


def test_gc_plan_matches_schema() -> None:
    plan = GcPlan(
        expected_generation=4,
        targets=[GcTarget(key="pool/main/d/dkc-linux/old.deb", sha256=SHA,
                          reason="series retired", size=10)],
        caps={"max_objects": 100, "max_bytes": 1_000_000},
        planned_utc=NOW,
    )
    schema.validate("gc-plan", plan.to_dict())


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


def test_records_serialize_deterministically() -> None:
    assert dumps(_manifest().to_dict()) == dumps(_manifest().to_dict())


def test_absent_optional_fields_do_not_appear() -> None:
    """An omitted field and an explicit null would otherwise hash differently."""
    text = dumps(_manifest().to_dict())
    assert "null" not in text
    assert "generation_snapshot_prefix" not in text


def test_record_hash_is_stable() -> None:
    assert _manifest().digest() == _manifest().digest()
    assert _manifest(generation=2).digest() != _manifest().digest()


# --------------------------------------------------------------------------
# Liveness versus audit references
# --------------------------------------------------------------------------


def test_liveness_comes_only_from_live_objects() -> None:
    manifest = _manifest(
        previous_publication={"publication_id": "20260810-99999999", "generation": 0},
        provenance_ref="state/publications/20260810-99999999/provenance.json",
    )
    assert manifest.live_objects == ["pool/main/d/dkc-linux/x.deb"]
    # An audit reference names a publication without keeping its payloads alive.
    assert "pool/main/d/dkc-linux/older.deb" not in manifest.live_objects


def test_manifest_predecessor_is_exactly_the_previous_generation() -> None:
    with pytest.raises(ValueError, match="not the predecessor"):
        _manifest(previous_publication={"publication_id": "20260810-99999999", "generation": 1})
    with pytest.raises(ValueError, match="must identify its predecessor"):
        _manifest(previous_publication=None)
    assert _manifest(generation=0, previous_publication=None).generation == 0


def test_live_objects_must_be_published_artifacts() -> None:
    with pytest.raises(ValueError, match="live objects not present"):
        _manifest(live_objects=["pool/main/d/dkc-linux/never-uploaded.deb"])


def test_a_key_cannot_be_live_and_queued_for_deletion() -> None:
    """The invariant that stops a suspended GC run from deleting served content."""
    with pytest.raises(ValueError, match="both live and queued"):
        _manifest(
            gc_queue=[GcQueueEntry(key="pool/main/d/dkc-linux/x.deb",
                                   sha256=SHA, size=1,
                                   reason="retired")]
        )


def test_retention_keeps_at_most_three_series() -> None:
    with pytest.raises(ValueError, match="three upstream series"):
        _manifest(retained_series=[[7, 1], [7, 0], [6, 18], [6, 12]])


def test_retained_series_are_ordered_newest_first() -> None:
    with pytest.raises(ValueError, match="newest first"):
        _manifest(retained_series=[[7, 0], [7, 1]])


def test_retention_mode_and_byte_limit_cannot_contradict() -> None:
    with pytest.raises(ValueError, match="must not carry"):
        _manifest(retention_mode="series", retention_max_bytes=10)
    with pytest.raises(ValueError, match="requires"):
        _manifest(retention_mode="series-size", retention_max_bytes=None)
    manifest = _manifest(
        retention_mode="series-size", retention_max_bytes=9_500_000_000
    )
    schema.validate("publication-manifest", manifest.to_dict())


def test_artifact_rejects_an_unknown_cache_class() -> None:
    with pytest.raises(ValueError, match="cache_class"):
        _artifact(cache="forever")


def test_tombstones_are_permanent() -> None:
    with pytest.raises(ValueError, match="permanent tombstone"):
        GcQueueEntry(key="pool/x", sha256=SHA, size=1,
                     reason="r", tombstoned=False)


@pytest.mark.parametrize("key", ["state/current.asc", "keys/archive.asc", "pool/", "pool//x"])
def test_gc_queue_accepts_only_exact_immutable_keys(key: str) -> None:
    with pytest.raises(ValueError):
        GcQueueEntry(key=key, sha256=SHA, size=1, reason="retired")


# --------------------------------------------------------------------------
# Discovery authority
# --------------------------------------------------------------------------


def test_a_public_hint_cannot_authorize_publication() -> None:
    with pytest.raises(ValueError, match="authoritative state read"):
        DiscoveryDecision(
            decision="build", source_version="7.1.7-1", source_dsc_sha256=SHA, utc=NOW,
            dkc_revision=1, build_policy_sha256="c" * 64, lto_mode="thin",
            publish_allowed=True, authoritative_state_read=False,
        )


def test_blocked_never_carries_work_or_permission() -> None:
    with pytest.raises(ValueError, match="blocked decision"):
        DiscoveryDecision(
            decision="blocked", source_version="7.1.7-1", source_dsc_sha256=SHA, utc=NOW,
            dkc_revision=1, build_policy_sha256="c" * 64, lto_mode="thin",
            build_required=True,
        )


@pytest.mark.parametrize(
    ("decision", "fields"),
    (
        ("no_op", {"publish_allowed": True, "authoritative_state_read": True,
                   "state_generation": 1,
                   "state_publication_id": "20260817-abcdef12"}),
        ("build", {"authoritative_state_read": True}),
        ("maintenance", {"build_required": True, "maintenance_required": True,
                         "publish_allowed": True, "authoritative_state_read": True,
                         "state_generation": 1,
                         "state_publication_id": "20260817-abcdef12"}),
    ),
)
def test_decision_routing_flags_are_not_independent_booleans(
    decision: str, fields: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="contradictory"):
        DiscoveryDecision(
            decision=decision,  # type: ignore[arg-type]
            source_version="7.1.7-1",
            source_dsc_sha256=SHA,
            utc=NOW,
            dkc_revision=1,
            build_policy_sha256="c" * 64,
            lto_mode="thin",
            **fields,
        )


def test_missing_state_is_distinct_from_generation_zero() -> None:
    decision = DiscoveryDecision(
        decision="blocked", source_version="7.1.7-1", source_dsc_sha256=SHA, utc=NOW,
        dkc_revision=1, build_policy_sha256="c" * 64, lto_mode="thin",
        reason="authoritative state unreadable",
    )
    assert decision.state_generation is None
    assert "state_generation" not in decision.to_dict()


# --------------------------------------------------------------------------
# State pointer
# --------------------------------------------------------------------------


def test_pointer_key_must_match_its_publication() -> None:
    with pytest.raises(ValueError, match="does not match"):
        StatePointer(generation=1, publication_id=PUB,
                     manifest_key="state/publications/other/manifest.json",
                     manifest_sha256=SHA, committed_utc=NOW)


def test_generation_must_increase() -> None:
    with pytest.raises(ValueError, match="must increase"):
        StatePointer(generation=1, publication_id=PUB,
                     manifest_key=f"state/publications/{PUB}/manifest.json",
                     manifest_sha256=SHA, committed_utc=NOW, previous_generation=1)


def test_record_timestamps_are_real_canonical_utc() -> None:
    with pytest.raises(Exception):
        schema.validate(
            "gc-plan",
            {
                "schema": "dkc.gc-plan.v1",
                "expected_generation": 1,
                "targets": [],
                "caps": {"max_objects": 1, "max_bytes": 1},
                "planned_utc": "not-a-date",
            },
        )


# --------------------------------------------------------------------------
# Deletion safety
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "",
        "/pool/x",
        "pool/../../etc/passwd",
        "pool/*",
        "pool/",
        "pool//x",
        "pool/./x",
        "pool/x\\y",
        "dists/trixie/InRelease",
        "dists/trixie/main/binary-amd64/Packages",
        "keys/dkc-archive.asc",
        "state/current.asc",
        "state/locks/production.json",
        "manifest.json",
        " pool/x",
    ],
)
def test_deletion_refuses_unsafe_keys(key: str) -> None:
    with pytest.raises(ValueError):
        GcTarget(key=key, sha256=SHA, reason="r", size=1)


@pytest.mark.parametrize(
    "key",
    [
        "pool/main/d/dkc-linux/dkc-linux-image.deb",
        "dists/trixie/main/binary-amd64/by-hash/SHA256/" + "a" * 64,
        "state/publications/20260101-aaaaaaaa/manifest.json",
        "state/transactions/20260101-aaaaaaaa/record.json",
    ],
)
def test_deletion_accepts_immutable_keys(key: str) -> None:
    GcTarget(key=key, sha256=SHA, reason="r", size=1)


def test_plan_enforces_object_cap() -> None:
    targets = [GcTarget(key=f"pool/x{i}", sha256=SHA, reason="r", size=1) for i in range(3)]
    with pytest.raises(ValueError, match="over the cap"):
        GcPlan(expected_generation=1, targets=targets,
               caps={"max_objects": 2, "max_bytes": 100}, planned_utc=NOW)


def test_plan_enforces_byte_cap() -> None:
    with pytest.raises(ValueError, match="bytes, over the cap"):
        GcPlan(expected_generation=1,
               targets=[GcTarget(key="pool/x", sha256=SHA, reason="r", size=101)],
               caps={"max_objects": 10, "max_bytes": 100}, planned_utc=NOW)


def test_target_refuses_negative_size() -> None:
    with pytest.raises(ValueError, match="negative"):
        GcTarget(key="pool/x", sha256=SHA, reason="r", size=-1)


def test_transaction_refuses_duplicate_or_malformed_phases() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        TransactionRecord(
            transaction_id=TXN,
            publication_id=PUB,
            expected_generation=1,
            intended_inrelease_sha256=SHA,
            started_utc=NOW,
            phases=[{"phase": 8, "state": "committed"}] * 2,
        )
    with pytest.raises(ValueError, match="phase number"):
        TransactionRecord(
            transaction_id=TXN,
            publication_id=PUB,
            expected_generation=1,
            intended_inrelease_sha256=SHA,
            started_utc=NOW,
            phases=[{"phase": 0, "state": "committed"}],
        )


def test_mutable_preconditions_bind_etags_to_their_kind() -> None:
    base = dict(
        transaction_id=TXN,
        publication_id=PUB,
        expected_generation=1,
        intended_inrelease_sha256=SHA,
        started_utc=NOW,
    )
    with pytest.raises(ValueError, match="requires a non-empty ETag"):
        TransactionRecord(
            **base,
            mutable_preconditions=[
                {"key": "dists/trixie/InRelease", "precondition": "if-match", "etag": None}
            ],
        )


def test_plan_refuses_duplicate_keys() -> None:
    target = GcTarget(key="pool/x", sha256=SHA, reason="r", size=1)
    with pytest.raises(ValueError, match="duplicate"):
        GcPlan(expected_generation=1, targets=[target, target],
               caps={"max_objects": 10, "max_bytes": 100}, planned_utc=NOW)
