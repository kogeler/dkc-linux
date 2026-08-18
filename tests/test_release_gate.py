from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import dkc.release_gate as release_gate
from dkc.records import DiscoveryDecision
from dkc.release_gate import (
    discovery_decision_outputs,
    require_publication_matches_decision,
    require_signing_request_matches_decision,
)


def _write_exact(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir(parents=True)
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (root / "evidence.sha256").write_text(
        "".join(
            f"{hashlib.sha256(body).hexdigest()}  {name}\n"
            for name, body in sorted(files.items())
        ),
        encoding="utf-8",
    )


def _decision(root: Path) -> None:
    value = {
        "authoritative_state_read": True,
        "build_policy_sha256": "b" * 64,
        "build_required": True,
        "decision": "build",
        "dkc_revision": 2,
        "lto_mode": "thin",
        "retention_mode": "series-size",
        "retention_max_bytes": 9_500_000_000,
        "maintenance_required": False,
        "publish_allowed": True,
        "reason": "new revision",
        "schema": "dkc.discovery-decision.v1",
        "source_dsc_sha256": "a" * 64,
        "source_version": "7.1.7-1",
        "state_generation": 4,
        "state_publication_id": "20260816-abcdef12",
        "utc": "2026-08-17T12:00:00Z",
    }
    body = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    outputs = "".join(
        f"{key}={field}\n"
        for key, field in sorted(
            discovery_decision_outputs(DiscoveryDecision(**value)).items()
        )
    ).encode()
    _write_exact(
        root,
        {
            "decision.json": body,
            "outputs.env": outputs,
            "result.env": b"status=PASS\nlifecycle_decision=build\n",
        },
    )


def _manifest() -> dict[str, object]:
    return {
        "artifacts": [],
        "apt_metadata": {
            "date": "2026-08-17T12:00:00Z",
            "index_hashes": {},
            "inrelease_sha256": "0" * 64,
            "valid_until": "2026-08-31T12:00:00Z",
        },
        "build_id": "0123456789ab",
        "build_policy_sha256": "b" * 64,
        "created_utc": "2026-08-17T12:00:00Z",
        "dkc_revision": 2,
        "dkc_version": "7.1.7-1+dkc13.2",
        "generation": 5,
        "live_objects": [],
        "lto_mode": "thin",
        "meta_packages": {},
        "publication_id": "20260817-abcdef12",
        "previous_publication": {
            "publication_id": "20260816-abcdef12",
            "generation": 4,
        },
        "retained_series": [[7, 1]],
        "retention_mode": "series-size",
        "retention_max_bytes": 9_500_000_000,
        "schema": "dkc.publication-manifest.v1",
        "source_dsc_sha256": "a" * 64,
        "source_version": "7.1.7-1",
        "transaction_id": "20260817-fedcba98",
    }


def test_publication_gate_binds_verified_repository_to_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decision = tmp_path / "decision"
    repository = tmp_path / "repository-result"
    _decision(decision)
    (repository / "repository").mkdir(parents=True)
    (repository / "repository" / "manifest.json").write_text(
        json.dumps(_manifest()), encoding="utf-8"
    )
    monkeypatch.setattr(release_gate, "load_verified_repository", lambda _: ())
    require_publication_matches_decision(decision, repository)

    changed = _manifest()
    changed["lto_mode"] = "full"
    (repository / "repository" / "manifest.json").write_text(
        json.dumps(changed), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="lto_mode"):
        require_publication_matches_decision(decision, repository)


def _signing_request() -> dict[str, object]:
    return {
        "schema": "dkc.repository-signing-request.v1",
        "status": "READY",
        "generation": 5,
        "issued_epoch": 1_787_875_200,
        "release_date": "Mon, 24 Aug 2026 12:00:00 GMT",
        "valid_until": "Mon, 07 Sep 2026 12:00:00 GMT",
        "primary_fingerprint": "A" * 40,
        "signing_subkey_fingerprints": ["B" * 40],
        "active_signing_subkey_fingerprint": "B" * 40,
        "source_version": "7.1.7-1",
        "source_dsc_sha256": "a" * 64,
        "dkc_version": "7.1.7-1+dkc13.2",
        "dkc_revision": 2,
        "build_policy_sha256": "b" * 64,
        "lto_mode": "thin",
        "build_id": "0" * 12,
        "retained_series": [[7, 1]],
        "retention_mode": "series-size",
        "retention_max_bytes": 9_500_000_000,
        "meta_packages": {
            f"dkc-linux-{role}-{flavor}-amd64": "7.1.7-1+dkc13.2"
            for role in ("base", "image", "headers")
            for flavor in ("v2", "v3")
        },
        "package_count": 19,
        "source_count": 2,
        "gc_queue": [],
        "previous_publication": {
            "publication_id": "20260816-abcdef12",
            "generation": 4,
        },
        "artifacts": [
            {
                "key": "pool/main/d/dkc-linux/package.deb",
                "sha256": "c" * 64,
                "size": 1,
                "media_type": "application/vnd.debian.binary-package",
                "cache_class": "immutable",
            }
        ],
    }


def test_signing_gate_binds_request_before_private_key_use(tmp_path: Path) -> None:
    decision = tmp_path / "decision"
    request_path = tmp_path / "signing-request.json"
    _decision(decision)
    request = _signing_request()
    request_path.write_text(json.dumps(request), encoding="utf-8")
    require_signing_request_matches_decision(decision, request_path)

    request["generation"] = 6
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="generation"):
        require_signing_request_matches_decision(decision, request_path)

    request = _signing_request()
    request["dkc_version"] = "7.1.7-1+dkc13.99"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="dkc_version"):
        require_signing_request_matches_decision(decision, request_path)

    request = _signing_request()
    request["retention_max_bytes"] = 9_400_000_000
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="retention_max_bytes"):
        require_signing_request_matches_decision(decision, request_path)

    request = _signing_request()
    previous = request["previous_publication"]
    assert isinstance(previous, dict)
    previous["publication_id"] = "20260815-abcdef12"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    with pytest.raises(ValueError, match="previous_publication"):
        require_signing_request_matches_decision(decision, request_path)
