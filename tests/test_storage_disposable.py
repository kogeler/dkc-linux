from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from dkc.s3 import PreconditionFailed, RemoteObject
from dkc.storage import ObjectMetadata
from dkc.storage_disposable import (
    DisposableConfig,
    DisposableIntegration,
    cleanup_disposable_prefix,
)
from dkc.storage_repository import (
    IMMUTABLE_CACHE,
    MUTABLE_CACHE,
    RepositoryObject,
    build_disposable_prefix,
    load_verified_repository,
    validate_disposable_prefix,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_repository_sums(repository: Path) -> None:
    excluded = {"SHA256SUMS", "SHA256SUMS.asc"}
    files = sorted(
        path
        for path in repository.rglob("*")
        if path.is_file() and path.relative_to(repository).as_posix() not in excluded
    )
    (repository / "SHA256SUMS").write_text(
        "".join(
            f"{_digest(path)}  {path.relative_to(repository).as_posix()}\n"
            for path in files
        ),
        encoding="utf-8",
    )


def _write_evidence(result: Path) -> None:
    checksum = result / "evidence" / "evidence.sha256"
    files = sorted(path for path in result.rglob("*") if path.is_file() and path != checksum)
    checksum.write_text(
        "".join(
            f"{_digest(path)}  ./{path.relative_to(result).as_posix()}\n"
            for path in files
        ),
        encoding="utf-8",
    )


def _verified_result(tmp_path: Path) -> Path:
    result = tmp_path / "result"
    repository = result / "repository"
    evidence = result / "evidence"
    (repository / "pool").mkdir(parents=True)
    (repository / "state").mkdir()
    evidence.mkdir()
    payload = repository / "pool" / "package.deb"
    payload.write_bytes(b"package")
    manifest = {
        "artifacts": [
            {
                "cache_class": "immutable",
                "key": "pool/package.deb",
                "media_type": "application/vnd.debian.binary-package",
                "sha256": _digest(payload),
                "size": payload.stat().st_size,
            }
        ],
        "apt_metadata": {
            "date": "2026-08-16T12:00:00Z",
            "index_hashes": {},
            "inrelease_sha256": "0" * 64,
            "valid_until": "2026-08-30T12:00:00Z",
        },
        "build_id": "0123456789ab",
        "created_utc": "2026-08-16T12:00:00Z",
        "dkc_revision": 1,
        "build_policy_sha256": "b" * 64,
        "lto_mode": "thin",
        "dkc_version": "1.0",
        "generation": 0,
        "live_objects": ["pool/package.deb"],
        "meta_packages": {},
        "publication_id": "20260816-abcdef12",
        "retained_series": [[7, 1]],
        "schema": "dkc.publication-manifest.v1",
        "source_version": "7.1.7-1",
        "source_dsc_sha256": "a" * 64,
        "transaction_id": "20260816-fedcba98",
    }
    (repository / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (repository / "manifest.json.asc").write_text("signature\n", encoding="utf-8")
    (repository / "state" / "current.asc").write_text(
        "state signature\n", encoding="utf-8"
    )
    _write_repository_sums(repository)
    (repository / "SHA256SUMS.asc").write_text("checksum signature\n", encoding="utf-8")
    (evidence / "result.env").write_text(
        "\n".join(
            (
                "status=PASS",
                "repository_assembly=PASS",
                "repository_signing=PASS",
                "signature_handoff=PASS",
                "signed_apt_client=PASS",
                "source_packages=PASS",
                "by_hash=PASS",
                "publishable=false",
                "",
            )
        ),
        encoding="utf-8",
    )
    _write_evidence(result)
    return result


def test_verified_repository_inventory_preserves_hashes_and_http_metadata(
    tmp_path: Path,
) -> None:
    inventory = load_verified_repository(_verified_result(tmp_path))
    by_key = {item.relative_key: item for item in inventory}
    assert set(by_key) == {
        "SHA256SUMS",
        "SHA256SUMS.asc",
        "manifest.json",
        "manifest.json.asc",
        "pool/package.deb",
        "state/current.asc",
    }
    assert by_key["pool/package.deb"].metadata == ObjectMetadata(
        "application/vnd.debian.binary-package", IMMUTABLE_CACHE
    )
    assert by_key["state/current.asc"].metadata.cache_control == MUTABLE_CACHE
    assert by_key["manifest.json"].metadata.content_type == "application/json"


def test_repository_inventory_rejects_unclassified_files(tmp_path: Path) -> None:
    result = _verified_result(tmp_path)
    repository = result / "repository"
    (repository / "unexpected").write_bytes(b"not classified")
    _write_repository_sums(repository)
    _write_evidence(result)
    with pytest.raises(ValueError, match="unclassified"):
        load_verified_repository(result)


@pytest.mark.parametrize(
    ("repository", "run_id", "nonce"),
    [
        ("owner/repo/extra", "run", "0" * 32),
        ("owner/repo", "../run", "0" * 32),
        ("owner/repo", "run", "short"),
        ("owner/repo", "run", "A" * 32),
    ],
)
def test_disposable_prefix_rejects_ambiguous_inputs(
    repository: str, run_id: str, nonce: str
) -> None:
    with pytest.raises(ValueError):
        build_disposable_prefix(repository, run_id, nonce)


def test_disposable_prefix_round_trip_validation() -> None:
    prefix = build_disposable_prefix(
        "owner/repo", "20260816T120000Z-deadbeef", "0" * 32
    )
    assert validate_disposable_prefix(prefix) == prefix
    with pytest.raises(ValueError, match="unsafe"):
        validate_disposable_prefix("production/repository/")


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, RemoteObject] = {}
        self.lock = threading.Lock()
        self.revision = 0

    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str:
        if if_none_match == (if_match is not None):
            raise ValueError("exactly one precondition required")
        with self.lock:
            current = self.objects.get(key)
            if if_none_match and current is not None:
                raise PreconditionFailed(f"put {key}")
            if if_match is not None and (current is None or current.etag != if_match):
                raise PreconditionFailed(f"put {key}")
            self.revision += 1
            etag = f'"revision-{self.revision}"'
            self.objects[key] = RemoteObject(bytes(body), metadata, etag)
            return etag

    def get(self, key: str) -> RemoteObject:
        with self.lock:
            return self.objects[key]

    def delete(self, key: str) -> None:
        with self.lock:
            self.objects.pop(key, None)

    def list_keys(
        self, prefix: str, *, page_size: int | None = None
    ) -> tuple[str, ...]:
        del page_size
        with self.lock:
            return tuple(sorted(key for key in self.objects if key.startswith(prefix)))


def _disposable_config() -> DisposableConfig:
    return DisposableConfig(
        canonical_repository="owner/repo",
        run_id="20260816T120000Z-deadbeef",
        nonce="0" * 32,
    )


def _inventory(tmp_path: Path) -> tuple[RepositoryObject, ...]:
    path = tmp_path / "package.deb"
    path.write_bytes(b"package")
    return (
        RepositoryObject(
            relative_key="pool/package.deb",
            path=path,
            sha256=_digest(path),
            size=path.stat().st_size,
            metadata=ObjectMetadata(
                "application/vnd.debian.binary-package", IMMUTABLE_CACHE
            ),
        ),
    )


def test_complete_disposable_flow_proves_cas_reads_and_zero_leftovers(
    tmp_path: Path,
) -> None:
    store = MemoryStore()
    integration = DisposableIntegration(store, _disposable_config())
    events = integration.run(_inventory(tmp_path))
    assert store.objects == {}
    assert any(event.operation == "cas-race" and event.status == "PASS" for event in events)
    assert any(
        event.operation == "authoritative-read" and event.status == "PASS"
        for event in events
    )
    assert any(event.operation == "paginated-list" for event in events)
    assert events[-1].detail == "zero objects"
    assert all(event.key.startswith(_disposable_config().prefix) for event in events)


def test_failure_still_removes_only_the_exact_disposable_prefix(
    tmp_path: Path,
) -> None:
    class FailingReadStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def get(self, key: str) -> RemoteObject:
            if key.endswith("repository/pool/package.deb") and not self.failed:
                self.failed = True
                raise RuntimeError("injected authoritative read failure")
            return super().get(key)

    store = FailingReadStore()
    outside = "production/keep"
    store.put(
        outside,
        b"keep",
        ObjectMetadata("application/octet-stream", IMMUTABLE_CACHE),
        if_none_match=True,
    )
    integration = DisposableIntegration(store, _disposable_config())
    with pytest.raises(RuntimeError, match="injected authoritative read failure"):
        integration.run(_inventory(tmp_path))
    assert set(store.objects) == {outside}


def test_recovery_cleanup_removes_recorded_prefix_only() -> None:
    store = MemoryStore()
    prefix = _disposable_config().prefix
    metadata = ObjectMetadata("application/octet-stream", IMMUTABLE_CACHE)
    store.put(f"{prefix}one", b"one", metadata, if_none_match=True)
    store.put("production/keep", b"keep", metadata, if_none_match=True)
    events = cleanup_disposable_prefix(store, prefix)
    assert set(store.objects) == {"production/keep"}
    assert events[-1].detail == "zero objects"
    with pytest.raises(ValueError, match="unsafe"):
        cleanup_disposable_prefix(store, "production/")


def test_missing_storage_secret_blocks_before_repository_or_network_access(
    tmp_path: Path,
) -> None:
    connection = {
        "s3_access_key_id": "access",
        "s3_addressing_style": "path",
        "s3_bucket": "test-bucket",
        "s3_endpoint": "https://objects.example.net",
        "s3_region": "test-region",
        "s3_secret_access_key": "",
    }
    connection_path = tmp_path / "connection.json"
    connection_path.write_text(json.dumps(connection), encoding="utf-8")
    connection_path.chmod(0o600)
    output = tmp_path / "output"
    repository_that_must_not_be_read = tmp_path / "absent-repository"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [
            sys.executable,
            "scripts/in-container/storage-disposable.py",
            "--repository-result",
            str(repository_that_must_not_be_read),
            "--connection",
            str(connection_path),
            "--output",
            str(output),
            "--run-id",
            "20260816T120000Z-deadbeef",
            "--canonical-repository",
            "owner/repo",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "BLOCKED" in result.stderr
    assert "absent-repository" not in result.stderr
    assert (output / "result.env").read_text().startswith("status=BLOCKED\n")
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "BLOCKED"
    assert "required connection fields are empty" in summary["error"]
    assert not any(
        value and value in summary["error"] for value in connection.values()
    )


def test_valid_connection_values_never_enter_failure_output_or_evidence(
    tmp_path: Path,
) -> None:
    connection = {
        "s3_access_key_id": "canary-access-987654321",
        "s3_addressing_style": "path",
        "s3_bucket": "canary-bucket-987654321",
        "s3_endpoint": "https://canary-endpoint-987654321.example.invalid",
        "s3_region": "canary-region-987654321",
        "s3_secret_access_key": "canary-secret-987654321",
        "s3_session_token": "canary-session-987654321",
    }
    connection_path = tmp_path / "connection.json"
    connection_path.write_text(json.dumps(connection), encoding="utf-8")
    connection_path.chmod(0o600)
    output = tmp_path / "output"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [
            sys.executable,
            "scripts/in-container/storage-disposable.py",
            "--repository-result",
            str(tmp_path / "repository-does-not-exist"),
            "--connection",
            str(connection_path),
            "--output",
            str(output),
            "--run-id",
            "20260817T120000Z-deadbeef",
            "--canonical-repository",
            "owner/repository",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    emitted = result.stdout + result.stderr + "".join(
        path.read_text(encoding="utf-8")
        for path in output.iterdir()
        if path.is_file()
    )
    assert "FAIL disposable storage integration" in emitted
    assert not any(value in emitted for value in connection.values())
