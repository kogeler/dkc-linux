from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from dkc.records import StatePointer
from dkc.s3 import RemoteObject
from dkc.serialize import canonical_bytes
from dkc.storage import ObjectMetadata


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
