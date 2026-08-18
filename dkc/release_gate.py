"""Cross-handoff release invariants independent of any CI provider."""

from __future__ import annotations

import json
from pathlib import Path

from .evidence import verify_evidence_directory
from .handoffs import load_authoritative_state_handoff
from .naming import Identity
from .records import DiscoveryDecision
from .schema import validate
from .serialize import boolean_text
from .storage_repository import load_verified_repository

__all__ = [
    "discovery_decision_outputs",
    "load_discovery_decision",
    "require_publication_matches_decision",
    "require_signing_request_matches_decision",
    "require_state_generation",
]


def discovery_decision_outputs(decision: DiscoveryDecision) -> dict[str, str]:
    next_generation = (
        0 if decision.state_generation is None else decision.state_generation + 1
    )
    return {
        "authoritative_state_read": boolean_text(decision.authoritative_state_read),
        "build_policy_sha256": decision.build_policy_sha256,
        "build_required": boolean_text(decision.build_required),
        "decision": decision.decision,
        "dkc_revision": str(decision.dkc_revision),
        "lto_mode": decision.lto_mode,
        "maintenance_required": boolean_text(decision.maintenance_required),
        "next_generation": str(next_generation),
        "publish_allowed": boolean_text(decision.publish_allowed),
        "source_dsc_sha256": decision.source_dsc_sha256,
        "source_version": decision.source_version,
        "state_present": boolean_text(decision.state_generation is not None),
    }


def load_discovery_decision(root: Path) -> DiscoveryDecision:
    paths = verify_evidence_directory(root)
    if paths != ("decision.json", "outputs.env", "result.env"):
        raise ValueError("lifecycle decision has an unexpected file boundary")
    try:
        value = json.loads((root / "decision.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("lifecycle decision is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("lifecycle decision is not an object")
    validate("discovery-decision", value)
    decision = DiscoveryDecision(**value)
    expected_outputs = "".join(
        f"{key}={field}\n"
        for key, field in sorted(discovery_decision_outputs(decision).items())
    )
    if (root / "outputs.env").read_text(encoding="utf-8") != expected_outputs:
        raise ValueError("lifecycle outputs differ from the typed decision")
    expected_result = f"status=PASS\nlifecycle_decision={decision.decision}\n"
    if (root / "result.env").read_text(encoding="utf-8") != expected_result:
        raise ValueError("lifecycle decision status is not successful")
    return decision


def require_publication_matches_decision(
    decision_root: Path, repository_result: Path
) -> None:
    decision = load_discovery_decision(decision_root)
    if (
        decision.decision not in ("build", "maintenance")
        or not decision.publish_allowed
    ):
        raise ValueError("lifecycle decision does not authorize a publication")
    load_verified_repository(repository_result)
    manifest_path = repository_result / "repository/manifest.json"
    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("verified repository manifest is not valid JSON") from exc
    if not isinstance(manifest_value, dict):
        raise ValueError("verified repository manifest is not an object")
    validate("publication-manifest", manifest_value)

    expected_generation = (
        0 if decision.state_generation is None else decision.state_generation + 1
    )
    expected_dkc_version = Identity.create(
        decision.source_version,
        decision.dkc_revision,
        str(manifest_value.get("build_id", "")),
    ).package_version
    expected = {
        "source_version": decision.source_version,
        "source_dsc_sha256": decision.source_dsc_sha256,
        "dkc_version": expected_dkc_version,
        "dkc_revision": decision.dkc_revision,
        "build_policy_sha256": decision.build_policy_sha256,
        "lto_mode": decision.lto_mode,
        "retention_mode": decision.retention_mode,
        "retention_max_bytes": decision.retention_max_bytes,
        "generation": expected_generation,
    }
    mismatched = sorted(
        field for field, value in expected.items() if manifest_value.get(field) != value
    )
    expected_previous = (
        None
        if decision.state_generation is None
        else {
            "publication_id": decision.state_publication_id,
            "generation": decision.state_generation,
        }
    )
    if manifest_value.get("previous_publication") != expected_previous:
        mismatched.append("previous_publication")
    if mismatched:
        raise ValueError(
            "verified repository differs from its lifecycle decision: "
            + ", ".join(mismatched)
        )


def require_signing_request_matches_decision(
    decision_root: Path, request_path: Path
) -> None:
    """Authorize one exact signing request from a typed lifecycle decision."""

    decision = load_discovery_decision(decision_root)
    if (
        decision.decision not in ("build", "maintenance")
        or not decision.publish_allowed
    ):
        raise ValueError("lifecycle decision does not authorize signing")
    if not request_path.is_file() or request_path.is_symlink():
        raise ValueError("repository signing request is not a plain file")
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("repository signing request is not valid JSON") from exc
    if not isinstance(request, dict):
        raise ValueError("repository signing request is not an object")
    validate("repository-signing-request", request)
    expected_generation = (
        0 if decision.state_generation is None else decision.state_generation + 1
    )
    expected_dkc_version = Identity.create(
        decision.source_version,
        decision.dkc_revision,
        str(request["build_id"]),
    ).package_version
    expected = {
        "source_version": decision.source_version,
        "source_dsc_sha256": decision.source_dsc_sha256,
        "dkc_version": expected_dkc_version,
        "dkc_revision": decision.dkc_revision,
        "build_policy_sha256": decision.build_policy_sha256,
        "lto_mode": decision.lto_mode,
        "retention_mode": decision.retention_mode,
        "retention_max_bytes": decision.retention_max_bytes,
        "generation": expected_generation,
    }
    mismatched = sorted(
        field for field, value in expected.items() if request.get(field) != value
    )
    previous = request.get("previous_publication")
    if decision.state_generation is None:
        if previous is not None:
            mismatched.append("previous_publication")
    elif not isinstance(previous, dict) or previous.get(
        "generation"
    ) != decision.state_generation or previous.get(
        "publication_id"
    ) != decision.state_publication_id:
        mismatched.append("previous_publication")
    if mismatched:
        raise ValueError(
            "repository signing request differs from its lifecycle decision: "
            + ", ".join(sorted(set(mismatched)))
        )


def require_state_generation(
    state_root: Path,
    expected_generation: int,
    *,
    keyring: Path,
    signing_subkeys: Path,
) -> None:
    if expected_generation < 0:
        raise ValueError("expected state generation must not be negative")
    state = load_authoritative_state_handoff(
        state_root,
        keyring=keyring,
        signing_subkeys=signing_subkeys,
    )
    if state is None:
        raise ValueError("authoritative state is not present")
    if state.pointer.generation != expected_generation:
        raise ValueError("authoritative state has an unexpected generation")
