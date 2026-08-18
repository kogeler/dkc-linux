"""Strict handoff checks around the isolated APT signing boundary."""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from dkc.schema import validate
from dkc.naming import FLAVORS, Identity, package_names


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "in-container" / "build-signed-repository.py"
SPEC = importlib.util.spec_from_file_location("build_signed_repository", SCRIPT)
assert SPEC and SPEC.loader
repository = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(repository)

PRIMARY = "A" * 40
SUBKEY = "B" * 40
EPOCH = 1_787_875_200


def _publication_identity() -> dict[str, object]:
    identity = Identity.create("7.1.7-1", 1, "c" * 12)
    return {
        "abi": identity.abi,
        "kernel_releases": {
            flavor: identity.kernel_release(flavor) for flavor in FLAVORS
        },
        "package_names": package_names(identity),
    }


def _write(root: pathlib.Path, relative: str, payload: bytes) -> pathlib.Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _handoff(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, object], dict[str, pathlib.Path]]:
    issued = datetime.fromtimestamp(EPOCH, timezone.utc)
    release_date = format_datetime(issued, usegmt=True)
    valid_until = format_datetime(
        issued + timedelta(seconds=repository.VALIDITY_SECONDS), usegmt=True
    )
    root = tmp_path / "repository"
    _write(root, "pool/main/d/dkc-linux/package.deb", b"package\n")
    _write(
        root,
        "dists/trixie/Release",
        f"Date: {release_date}\nValid-Until: {valid_until}\n".encode(),
    )
    keyring = _write(root, "keys/dkc-archive-keyring.gpg", b"public-key\n")
    primary = _write(root, "keys/archive-primary.fingerprint", f"{PRIMARY}\n".encode())
    subkeys = _write(
        root, "keys/archive-signing-subkeys.fingerprints", f"{SUBKEY}\n".encode()
    )
    request: dict[str, object] = {
        "schema": "dkc.repository-signing-request.v1",
        "status": "READY",
        "generation": 0,
        "issued_epoch": EPOCH,
        "release_date": release_date,
        "valid_until": valid_until,
        "primary_fingerprint": PRIMARY,
        "signing_subkey_fingerprints": [SUBKEY],
        "active_signing_subkey_fingerprint": SUBKEY,
        "source_version": "7.1.7-1",
        "source_dsc_sha256": "d" * 64,
        "dkc_version": "7.1.7-1+dkc13.1",
        "dkc_revision": 1,
        "build_policy_sha256": "e" * 64,
        "lto_mode": "thin",
        "build_id": "c" * 12,
        "retained_series": [[7, 1]],
        "retention_mode": "series-size",
        "retention_max_bytes": 9_500_000_000,
        "meta_packages": {
            f"dkc-linux-{role}-{flavor}-amd64": "7.1.7-1+dkc13.1"
            for role in ("base", "image", "headers")
            for flavor in ("v2", "v3")
        },
        "package_count": 19,
        "source_count": 2,
        "gc_queue": [],
        "artifacts": repository.repository_artifact_records(root),
    }
    return root, request, {"keyring": keyring, "primary": primary, "subkeys": subkeys}


def _validate(
    root: pathlib.Path, request: dict[str, object], tracked: dict[str, pathlib.Path]
) -> list[dict[str, object]]:
    return repository.validate_unsigned_handoff(
        root,
        request,
        public_keyring=tracked["keyring"],
        primary_fingerprint=tracked["primary"],
        signing_subkeys=tracked["subkeys"],
    )


def test_release_inventory_keeps_v4_buildable_but_outside_the_archive() -> None:
    source, release, meta = repository.release_package_inventory(
        _publication_identity()
    )
    assert len(source) == 26
    assert len(release) == 18
    assert len(meta) == 6
    assert any("-v4-amd64" in package for package in source)
    assert not any("-v4-amd64" in package for package in release)

    malformed = _publication_identity()
    names = malformed["package_names"]
    assert isinstance(names, dict) and isinstance(names["versioned"], list)
    names["versioned"][0] = "dkc-linux-invented-v2-amd64"
    with pytest.raises(ValueError, match="exact source package graph"):
        repository.release_package_inventory(malformed)


def test_series_retention_keeps_every_revision_in_the_newest_three_parsed_series(
    tmp_path: pathlib.Path,
) -> None:
    pool = tmp_path / "pool"
    pool.mkdir()
    versions = [
        "1:7.2~rc1-1+dkc13.1",
        "7.2.0-2+dkc13.2",
        "7.1.99-1+dkc13.3",
        "6.18.12-3+dkc13.1",
        "6.12.99-9+dkc13.4",
    ]
    for index, version in enumerate(versions):
        (pool / f"dkc-linux_{index}.dsc").write_text(
            f"Format: 3.0 (quilt)\nSource: dkc-linux\nVersion: {version}\n",
            encoding="utf-8",
        )
    retained = repository.apply_retention(
        pool, mode="series", max_bytes=None, fixed_bytes=0
    )
    assert retained.retained_series == ((7, 2), (7, 1), (6, 18))
    remaining = sorted(path.name for path in pool.iterdir())
    assert remaining == [
        "dkc-linux_0.dsc",
        "dkc-linux_1.dsc",
        "dkc-linux_2.dsc",
        "dkc-linux_3.dsc",
    ]


def test_strict_unsigned_handoff_accepts_only_its_exact_inventory(tmp_path: pathlib.Path) -> None:
    root, request, tracked = _handoff(tmp_path)
    assert _validate(root, request, tracked) == request["artifacts"]

    _write(root, "pool/main/d/dkc-linux/unrequested.deb", b"extra\n")
    with pytest.raises(ValueError, match="strict signing request"):
        _validate(root, request, tracked)


def test_strict_unsigned_handoff_rejects_files_outside_repository_roots(
    tmp_path: pathlib.Path,
) -> None:
    root, request, tracked = _handoff(tmp_path)
    _write(root, "unrequested.txt", b"extra\n")
    with pytest.raises(ValueError, match="unrequested path"):
        _validate(root, request, tracked)


def test_strict_unsigned_handoff_rejects_changed_bytes(tmp_path: pathlib.Path) -> None:
    root, request, tracked = _handoff(tmp_path)
    (root / "pool/main/d/dkc-linux/package.deb").write_bytes(b"replacement\n")
    with pytest.raises(ValueError, match="strict signing request"):
        _validate(root, request, tracked)


def test_signed_publication_ids_are_retry_safe_and_content_bound() -> None:
    first = repository.signed_publication_ids(
        stamp="20260817",
        generation=3,
        request=b"request-one",
        inrelease=b"inrelease",
        release_signature=b"release-signature",
    )
    repeated = repository.signed_publication_ids(
        stamp="20260817",
        generation=3,
        request=b"request-one",
        inrelease=b"inrelease",
        release_signature=b"release-signature",
    )
    retried = repository.signed_publication_ids(
        stamp="20260817",
        generation=3,
        request=b"request-two",
        inrelease=b"inrelease",
        release_signature=b"release-signature",
    )
    assert first == repeated
    assert first != retried
    assert first[0] != first[1]
    assert first[0].startswith("20260817-p")
    assert first[1].startswith("20260817-t")


def test_request_dates_and_active_subkey_are_semantic(tmp_path: pathlib.Path) -> None:
    root, request, tracked = _handoff(tmp_path)
    request["valid_until"] = "Fri, 01 Jan 2038 00:00:00 GMT"
    with pytest.raises(ValueError, match="issuance epoch"):
        _validate(root, request, tracked)

    root, request, tracked = _handoff(tmp_path / "second")
    predecessor = "C" * 40
    request["signing_subkey_fingerprints"] = [predecessor, SUBKEY]
    request["active_signing_subkey_fingerprint"] = predecessor
    with pytest.raises(ValueError, match="final signing subkey"):
        _validate(root, request, tracked)


def test_request_schema_rejects_unsafe_repository_paths(tmp_path: pathlib.Path) -> None:
    _root, request, _tracked = _handoff(tmp_path)
    records = request["artifacts"]
    assert isinstance(records, list) and isinstance(records[0], dict)
    records[0]["key"] = "pool/../outside"
    with pytest.raises(Exception):
        validate("repository-signing-request", request)


def test_request_schema_requires_the_exact_release_meta_set(tmp_path: pathlib.Path) -> None:
    _root, request, _tracked = _handoff(tmp_path)
    validate("repository-signing-request", request)
    meta = request["meta_packages"]
    assert isinstance(meta, dict)
    meta.pop("dkc-linux-headers-v3-amd64")
    meta["dkc-linux-image-v4-amd64"] = "7.1.7-1+dkc13.1"
    with pytest.raises(Exception):
        validate("repository-signing-request", request)


def test_unsigned_handoff_binds_every_meta_to_the_archive_version(
    tmp_path: pathlib.Path,
) -> None:
    root, request, tracked = _handoff(tmp_path)
    meta = request["meta_packages"]
    assert isinstance(meta, dict)
    meta["dkc-linux-image-v3-amd64"] = "7.1.7-1+dkc13.2"
    with pytest.raises(ValueError, match="exact release meta-package set"):
        _validate(root, request, tracked)


def test_online_key_inventory_requires_stub_primary_and_one_active_subkey(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def record(kind: str, fingerprint: str, marker: str, expires: int) -> list[str]:
        fields = [""] * 15
        fields[0] = kind
        fields[1] = "u"
        fields[5] = str(EPOCH)
        fields[6] = str(expires)
        fields[11] = "s" if kind == "ssb" else "c"
        fields[14] = marker
        fingerprint_fields = [""] * 10
        fingerprint_fields[0] = "fpr"
        fingerprint_fields[9] = fingerprint
        return [":".join(fields), ":".join(fingerprint_fields)]

    expiry = EPOCH + repository.VALIDITY_SECONDS + 100
    lines = record("sec", PRIMARY, "#", expiry) + record("ssb", SUBKEY, "+", expiry)
    monkeypatch.setattr(
        repository.subprocess,
        "check_output",
        lambda *args, **kwargs: "\n".join(lines) + "\n",
    )
    report = repository.check_secret_subkey(
        tmp_path,
        primary_fingerprint=PRIMARY,
        active_fingerprint=SUBKEY,
        valid_until_epoch=EPOCH + repository.VALIDITY_SECONDS,
        clock_skew_seconds=50,
        safety_seconds=50,
    )
    assert report["primary_secret"] == "UNAVAILABLE"
    assert report["available_secret_subkeys"] == 1
    assert report["primary_expires_epoch"] == expiry

    lines[0] = lines[0].removesuffix("#").removesuffix(":") + ":+"
    with pytest.raises(ValueError, match="primary secret must be unavailable"):
        repository.check_secret_subkey(
            tmp_path,
            primary_fingerprint=PRIMARY,
            active_fingerprint=SUBKEY,
            valid_until_epoch=EPOCH,
            clock_skew_seconds=0,
            safety_seconds=0,
        )


def test_provisioning_exports_only_the_online_subkey() -> None:
    script = (ROOT / "scripts" / "generate-archive-key.sh").read_text()
    assert "--export-secret-subkeys" in script
    assert "online_primary_secret=UNAVAILABLE" in script
    assert "primary revocation certificate verification failed" in script
    assert "APT_GPG_SIGNING_SUBKEY_B64" in script
    assert "! -path 'evidence/offline-material.sha256'" in script
    assert "trap cleanup_sensitive_scratch EXIT" in script
    assert 'rm -f -- "$passphrase_file" "$raw_subkey"' in script


def test_keyring_package_identity_does_not_use_repository_run_time() -> None:
    script = (
        ROOT / "scripts" / "in-container" / "assemble-apt-repository.sh"
    ).read_text()
    assert 'active_subkey="$(tail -n 1 "$signing_subkeys")"' in script
    assert 'keyring_version="1.0+$(date --utc --date="@${keyring_epoch}"' in script
    assert '"$keyring_version" "$keyring_epoch"' in script
    assert '"$keyring_version" "$epoch"' not in script


def test_per_invocation_source_metadata_stays_outside_the_apt_pool() -> None:
    assembler = (
        ROOT / "scripts" / "in-container" / "assemble-apt-repository.sh"
    ).read_text()
    repository_builder = SCRIPT.read_text()
    assert 'evidence/source-upload-metadata' in assembler
    assert 'and source.name.endswith((".dsc", ".orig.tar.xz", ".debian.tar.xz"))' in (
        repository_builder
    )
    assert 'if source.name.endswith((".deb", ".dsc", ".tar.xz"))' in (
        repository_builder
    )
