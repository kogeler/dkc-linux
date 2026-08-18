from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import dkc.handoffs as handoffs
from dkc.handoffs import (
    load_authoritative_state_handoff,
    load_live_pool_handoff,
    load_source_handoff,
)
from dkc.serialize import dumps
from dkc.source_discovery import build_inventory, make_variables


def _evidence(root: Path) -> None:
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "evidence.sha256":
            records.append(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                f"{path.relative_to(root).as_posix()}"
            )
    (root / "evidence.sha256").write_text("\n".join(records) + "\n")


def _source(root: Path) -> dict[str, object]:
    fixture = Path(__file__).parent / "fixtures/sources-linux-sid.txt"
    inventory = build_inventory(
        fixture.read_text(encoding="utf-8"),
        mirror="http://deb.debian.org/debian",
        discovered=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
    )
    root.mkdir()
    (root / "source-inventory.json").write_text(dumps(inventory))
    (root / "source.env").write_text(
        "".join(
            f"{key}={value}\n"
            for key, value in sorted(make_variables(inventory).items())
        )
    )
    (root / "result.env").write_text("status=PASS\nsource_discovery=PASS\n")
    _evidence(root)
    return inventory


def _manifest(*, package_body: bytes = b"package\n") -> dict[str, object]:
    package_hash = hashlib.sha256(package_body).hexdigest()
    return {
        "artifacts": [
            {
                "cache_class": "immutable",
                "key": "pool/main/d/dkc-linux/package.deb",
                "media_type": "application/vnd.debian.binary-package",
                "sha256": package_hash,
                "size": len(package_body),
            }
        ],
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
        "dkc_version": "7.1.7-1+dkc2.1",
        "generation": 5,
        "live_objects": ["pool/main/d/dkc-linux/package.deb"],
        "lto_mode": "thin",
        "meta_packages": {},
        "publication_id": "20260817-abcdef12",
        "previous_publication": {
            "generation": 4,
            "publication_id": "20260810-abcdef12",
        },
        "retained_series": [[7, 1]],
        "schema": "dkc.publication-manifest.v1",
        "source_dsc_sha256": "a" * 64,
        "source_version": "7.1.7-1",
        "transaction_id": "20260817-fedcba98",
    }


def _present_state(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    package_body: bytes = b"package\n",
):
    manifest_body = dumps(_manifest(package_body=package_body)).encode()
    pointer_body = dumps(
        {
            "committed_utc": "2026-08-17T12:00:00Z",
            "generation": 5,
            "manifest_key": "state/publications/20260817-abcdef12/manifest.json",
            "manifest_sha256": hashlib.sha256(manifest_body).hexdigest(),
            "previous_generation": 4,
            "publication_id": "20260817-abcdef12",
            "schema": "dkc.state-pointer.v1",
        }
    ).encode()
    (root / "state").mkdir(parents=True)
    (root / "state/current.asc").write_bytes(b"signed pointer\n")
    (root / "state/pointer.json").write_bytes(pointer_body)
    (root / "state/manifest.json").write_bytes(manifest_body)
    (root / "state/manifest.json.asc").write_bytes(b"manifest signature\n")
    (root / "state-status.json").write_text(
        dumps(
            {
                "authoritative": True,
                "manifest_etag": '"manifest"',
                "schema": "dkc.authoritative-state-read.v1",
                "state_etag": '"state"',
                "storage_object_count": 6,
                "storage_size": 1000,
                "status": "PRESENT",
            }
        )
    )
    (root / "result.env").write_text(
        "status=PASS\nauthoritative_state=PRESENT\n"
    )
    _evidence(root)
    keyring = root.parent / "keyring.gpg"
    fingerprints = root.parent / "fingerprints"
    keyring.write_bytes(b"keyring\n")
    fingerprints.write_text("A" * 40 + "\n")

    def verify_signature(
        _signature: Path,
        *,
        keyring: Path,
        fingerprints: frozenset[str],
        signed: Path | None = None,
        output: Path | None = None,
    ) -> None:
        assert keyring.is_file()
        assert fingerprints == frozenset({"A" * 40})
        if output is not None:
            output.write_bytes(pointer_body)
        if signed is not None:
            assert signed.name == "manifest.json"

    monkeypatch.setattr(handoffs, "_verify_signature", verify_signature)
    state = load_authoritative_state_handoff(
        root,
        keyring=keyring,
        signing_subkeys=fingerprints,
    )
    assert state is not None
    return state, keyring, fingerprints


def test_source_handoff_requires_exact_derived_successful_files(tmp_path: Path) -> None:
    root = tmp_path / "source"
    inventory = _source(root)
    assert load_source_handoff(root) == inventory
    (root / "source.env").write_text("DKC_SOURCE_VERSION=wrong\n")
    _evidence(root)
    with pytest.raises(ValueError, match="differs"):
        load_source_handoff(root)


def test_empty_state_handoff_has_one_exact_typed_shape(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "state-status.json").write_text(
        dumps(
            {
                "authoritative": True,
                "schema": "dkc.authoritative-state-read.v1",
                "storage_object_count": 0,
                "storage_size": 0,
                "status": "EMPTY",
            }
        )
    )
    (root / "result.env").write_text("status=PASS\nauthoritative_state=EMPTY\n")
    _evidence(root)
    assert load_authoritative_state_handoff(
        root,
        keyring=tmp_path / "unused-keyring",
        signing_subkeys=tmp_path / "unused-fingerprints",
    ) is None
    (root / "unexpected").write_text("data\n")
    _evidence(root)
    with pytest.raises(ValueError, match="file boundary"):
        load_authoritative_state_handoff(
            root,
            keyring=tmp_path / "unused-keyring",
            signing_subkeys=tmp_path / "unused-fingerprints",
        )


def test_present_state_and_pool_are_bound_to_signed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    package_body = b"package\n"
    state, _, _ = _present_state(state_root, monkeypatch, package_body=package_body)
    pool_root = tmp_path / "pool-result"
    package = pool_root / "pool/main/d/dkc-linux/package.deb"
    package.parent.mkdir(parents=True)
    package.write_bytes(package_body)
    (pool_root / "summary.json").write_text(
        dumps(
            {
                "object_count": 1,
                "schema": "dkc.pool-export.v1",
                "size": len(package_body),
                "state": "PRESENT",
                "status": "PASS",
            }
        )
    )
    _evidence(pool_root)
    assert load_live_pool_handoff(pool_root, state) == pool_root / "pool"
    package.write_bytes(b"changed\n")
    _evidence(pool_root)
    with pytest.raises(ValueError, match="signed identity"):
        load_live_pool_handoff(pool_root, state)


def test_empty_pool_handoff_is_exact_and_self_consistent(tmp_path: Path) -> None:
    root = tmp_path / "pool-result"
    (root / "pool").mkdir(parents=True)
    (root / "summary.json").write_text(
        dumps(
            {
                "object_count": 0,
                "schema": "dkc.pool-export.v1",
                "size": 0,
                "state": "EMPTY",
                "status": "PASS",
            }
        )
    )
    _evidence(root)
    assert load_live_pool_handoff(root, None) == root / "pool"

    (root / "pool/unexpected").write_text("data\n")
    _evidence(root)
    with pytest.raises(ValueError, match="empty live-pool"):
        load_live_pool_handoff(root, None)


def test_present_state_rejects_pointer_hash_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "state"
    _present_state(root, monkeypatch)
    pointer = json.loads((root / "state/pointer.json").read_text())
    pointer["manifest_sha256"] = "f" * 64
    changed_pointer = dumps(pointer).encode()
    (root / "state/pointer.json").write_bytes(changed_pointer)
    _evidence(root)

    def verify_signature(
        _signature: Path,
        *,
        keyring: Path,
        fingerprints: frozenset[str],
        signed: Path | None = None,
        output: Path | None = None,
    ) -> None:
        if output is not None:
            output.write_bytes(changed_pointer)

    monkeypatch.setattr(handoffs, "_verify_signature", verify_signature)
    with pytest.raises(ValueError, match="manifest hash"):
        load_authoritative_state_handoff(
            root,
            keyring=tmp_path / "keyring.gpg",
            signing_subkeys=tmp_path / "fingerprints",
        )
