from __future__ import annotations

from datetime import datetime, timezone

from dkc.lifecycle import decide
from dkc.records import Artifact, PublicationManifest, StatePointer
from dkc.state import AuthoritativeState, parse_manifest, parse_state_pointer
from dkc.serialize import canonical_bytes


UTC = timezone.utc
SHA = "a" * 64
PUB = "20260817-abcdef123456"


def state(
    *,
    version: str = "7.1.7-1",
    revision: int = 1,
    valid_until: str = "Mon, 31 Aug 2026 12:00:00 GMT",
    retention_mode: str = "series-size",
    retention_max_bytes: int | None = 9_500_000_000,
    storage_size: int = 1_000,
) -> AuthoritativeState:
    artifact = Artifact("pool/main/d/dkc-linux/x.deb", SHA, 1, "application/octet-stream", "immutable")
    manifest = PublicationManifest(
        generation=3,
        publication_id=PUB,
        transaction_id="20260817-123456abcdef",
        source_version=version,
        source_dsc_sha256=SHA,
        dkc_version=f"{version}+dkc13.1",
        dkc_revision=revision,
        build_policy_sha256="c" * 64,
        lto_mode="thin",
        build_id="b" * 12,
        retained_series=[[7, 1]],
        artifacts=[artifact],
        live_objects=[artifact.key],
        apt_metadata={
            "inrelease_sha256": SHA,
            "date": "Mon, 17 Aug 2026 12:00:00 GMT",
            "valid_until": valid_until,
            "index_hashes": {},
        },
        meta_packages={},
        created_utc="2026-08-17T12:00:00Z",
        retention_mode=retention_mode,  # type: ignore[arg-type]
        retention_max_bytes=retention_max_bytes,
        previous_publication={"publication_id": "20260816-abcdef12", "generation": 2},
    )
    pointer = StatePointer(
        3,
        PUB,
        f"state/publications/{PUB}/manifest.json",
        manifest.digest(),
        "2026-08-17T12:00:00Z",
        2,
    )
    return AuthoritativeState(
        pointer, manifest, '"state"', '"manifest"', storage_size=storage_size
    )


def decision(**overrides: object):
    values = {
        "source_version": "7.1.7-1",
        "source_dsc_sha256": SHA,
        "dkc_revision": 1,
        "build_policy_sha256": "c" * 64,
        "lto_mode": "thin",
        "retention_mode": "series-size",
        "retention_max_bytes": 9_500_000_000,
        "now": datetime(2026, 8, 17, 12, tzinfo=UTC),
        "state": state(),
        "state_read_succeeded": True,
        "bootstrap_allowed": False,
    }
    values.update(overrides)
    return decide(**values)  # type: ignore[arg-type]


def test_unavailable_state_is_blocked_not_bootstrap() -> None:
    result = decision(state=None, state_read_succeeded=False, bootstrap_allowed=True)
    assert result.decision == "blocked"
    assert not result.publish_allowed
    assert not result.authoritative_state_read


def test_empty_state_bootstrap_requires_explicit_permission() -> None:
    blocked = decision(state=None)
    allowed = decision(state=None, bootstrap_allowed=True)
    assert blocked.decision == "blocked"
    assert allowed.decision == "build"
    assert allowed.build_required and allowed.publish_allowed
    assert allowed.state_generation is None


def test_newer_source_builds_and_older_source_blocks() -> None:
    assert decision(source_version="7.1.8-1").decision == "build"
    older = decision(source_version="7.1.6-1")
    assert older.decision == "blocked"
    assert not older.publish_allowed


def test_same_version_with_changed_descriptor_hash_blocks() -> None:
    result = decision(source_dsc_sha256="c" * 64)
    assert result.decision == "blocked"
    assert not result.publish_allowed


def test_same_source_higher_downstream_revision_builds() -> None:
    result = decision(dkc_revision=2)
    assert result.decision == "build"
    assert result.build_required and result.publish_allowed


def test_same_source_lower_downstream_revision_blocks() -> None:
    result = decision(state=state(revision=2), dkc_revision=1)
    assert result.decision == "blocked"
    assert not result.publish_allowed


def test_policy_or_lto_change_requires_revision_increase() -> None:
    policy = decision(build_policy_sha256="d" * 64)
    lto = decision(lto_mode="full")
    assert policy.decision == "blocked"
    assert lto.decision == "blocked"
    assert "revision" in str(policy.reason)


def test_revision_increase_allows_policy_and_lto_change() -> None:
    result = decision(
        dkc_revision=2,
        build_policy_sha256="d" * 64,
        lto_mode="full",
    )
    assert result.decision == "build"


def test_fresh_metadata_is_noop_and_near_expiry_is_maintenance() -> None:
    assert decision().decision == "no_op"
    result = decision(state=state(valid_until="Mon, 24 Aug 2026 12:00:00 GMT"))
    assert result.decision == "maintenance"
    assert result.maintenance_required and not result.build_required


def test_retention_policy_change_or_exceeded_limit_requires_maintenance() -> None:
    legacy = decision(state=state(retention_mode="series", retention_max_bytes=None))
    oversized = decision(state=state(storage_size=9_500_000_001))
    assert legacy.decision == "maintenance"
    assert oversized.decision == "maintenance"
    assert legacy.maintenance_required and oversized.maintenance_required


def test_state_parsers_reconstruct_typed_records() -> None:
    expected = state()
    pointer = parse_state_pointer(canonical_bytes(expected.pointer.to_dict()))
    manifest = parse_manifest(canonical_bytes(expected.manifest.to_dict()))
    assert pointer == expected.pointer
    assert manifest == expected.manifest
