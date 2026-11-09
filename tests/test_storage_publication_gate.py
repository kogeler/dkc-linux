from __future__ import annotations

import importlib.util
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dkc.records import StatePointer
from dkc.s3 import ListedObject, RemoteObject
from dkc.serialize import canonical_bytes
from dkc.storage import ObjectMetadata
from dkc.storage_output import StorageRedactor, run_with_sanitized_output
from dkc.storage_progress import StorageProgress
from tests.fake_storage import ConditionalObjectStore
from tests.test_publication_plan import inventory


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/in-container/storage-publish.py"
SPEC = importlib.util.spec_from_file_location("storage_publish", SCRIPT)
assert SPEC and SPEC.loader
storage_publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(storage_publish)


def test_publication_cas_binds_the_exact_predecessor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    previous_id = "20260816-abcdef12"
    observed = StatePointer(
        generation=5,
        publication_id=previous_id,
        manifest_key=f"state/publications/{previous_id}/manifest.json",
        manifest_sha256="a" * 64,
        committed_utc="2026-08-16T12:00:00Z",
        previous_generation=4,
    )
    desired_id = "20260817-abcdef12"
    desired = StatePointer(
        generation=6,
        publication_id=desired_id,
        manifest_key=f"state/publications/{desired_id}/manifest.json",
        manifest_sha256="b" * 64,
        committed_utc="2026-08-17T12:00:00Z",
        previous_generation=5,
    )

    class Store:
        def get(self, key: str) -> RemoteObject | None:
            assert key == "state/current.asc"
            return RemoteObject(
                b"current signed pointer",
                ObjectMetadata(
                    "application/pgp-signature",
                    "public, max-age=0, must-revalidate",
                ),
                '"state"',
            )

    def verify(
        _signature: Path,
        *,
        keyring: Path,
        fingerprints: set[str],
        signed: Path | None = None,
        output: Path | None = None,
    ) -> None:
        del keyring, fingerprints, signed
        assert output is not None
        output.write_bytes(canonical_bytes(observed.to_dict()))

    monkeypatch.setattr(storage_publish, "verify", verify)
    expected_previous = {
        "publication_id": previous_id,
        "generation": observed.generation,
    }
    storage_publish.verify_current_generation(
        Store(),
        b"desired signed pointer",
        desired,
        SimpleNamespace(previous_publication=expected_previous),
        keyring=tmp_path / "keyring",
        fingerprints={"A" * 40},
        workspace=tmp_path,
    )

    with pytest.raises(ValueError, match="does not follow"):
        storage_publish.verify_current_generation(
            Store(),
            b"desired signed pointer",
            desired,
            SimpleNamespace(
                previous_publication={
                    "publication_id": "20260815-abcdef12",
                    "generation": observed.generation,
                }
            ),
            keyring=tmp_path / "keyring",
            fingerprints={"A" * 40},
            workspace=tmp_path,
        )


def test_publisher_wires_reuse_progress_lease_and_verifiable_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Store(ConditionalObjectStore):
        def list_objects(self, prefix: str):  # type: ignore[no-untyped-def]
            return tuple(
                ListedObject(key, len(self._objects[key].body))
                for key in self.list_keys(prefix)
            )

    store = Store()
    items = inventory(tmp_path)
    repository_result = tmp_path / "repository-result"
    state = repository_result / "repository/state/current.asc"
    state.parent.mkdir(parents=True)
    state.write_bytes(next(item.read() for item in items if item.relative_key == "state/current.asc"))
    token = tmp_path / "token"
    token.write_text("fake-token")
    subkeys = tmp_path / "subkeys"
    subkeys.write_text("A" * 40 + "\n")
    manifest = SimpleNamespace(
        generation=0, gc_queue=[], live_objects=[], retention_mode="series",
        retention_max_bytes=None,
    )
    monkeypatch.setattr(storage_publish, "S3Client", lambda *_: None)
    monkeypatch.setattr(storage_publish, "S3ObjectStore", lambda _: store)
    monkeypatch.setattr(storage_publish, "load_verified_repository", lambda _: items)
    monkeypatch.setattr(storage_publish, "desired_pointer", lambda *_, **__: (
        SimpleNamespace(generation=0, previous_generation=None, publication_id="test"), manifest
    ))
    monkeypatch.setattr(storage_publish, "load_connection", lambda _: SimpleNamespace(
        endpoint=None, credentials=None
    ))
    args = SimpleNamespace(
        output=tmp_path / "first", connection=tmp_path / "connection",
        github_token_file=token, repository_result=repository_result,
        signing_subkeys=subkeys, keyring=tmp_path / "keyring",
        canonical_repository="owner/repo", workflow_run_id="1", run_attempt="1",
        lease_ttl_seconds=900, takeover_grace_seconds=300,
        max_object_bytes=1_000_000, gc_max_objects=100, gc_max_bytes=1_000_000,
    )
    redactor = StorageRedactor(())
    for output in (tmp_path / "first", tmp_path / "retry"):
        args.output = output
        stream = io.StringIO()
        progress = StorageProgress(redactor, stream)
        assert run_with_sanitized_output(redactor, lambda: storage_publish.main(args, progress)) == 0
        summary = json.loads((output / "summary.json").read_text())
        assert summary["status"] == "PASS"
        assert summary["storage_size"] == sum(item.size for item in store.list_objects(""))
        assert "phase=verify-storage-size" in stream.getvalue()
        timings = json.loads((output / "progress.json").read_text())
        assert any(phase["phase"] == "verify-repository" for phase in timings["phases"])
        for line in (output / "evidence.sha256").read_text().splitlines():
            digest, name = line.split("  ", 1)
            assert hashlib.sha256((output / name).read_bytes()).hexdigest() == digest
    assert summary["immutable_transfers"]["reused_objects"] > 0
    assert summary["immutable_transfers"]["created_objects"] == 0
