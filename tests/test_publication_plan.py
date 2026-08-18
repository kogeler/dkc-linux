from __future__ import annotations

from pathlib import Path

import pytest

from dkc.publication_plan import plan_repository
from dkc.storage import ObjectMetadata
from dkc.storage_repository import RepositoryObject
from tests.fake_storage import ConditionalObjectStore


IMMUTABLE = ObjectMetadata("application/octet-stream", "public, max-age=31536000, immutable")
MUTABLE = ObjectMetadata("application/octet-stream", "public, max-age=0, must-revalidate")


def item(tmp_path: Path, key: str, body: bytes, metadata: ObjectMetadata) -> RepositoryObject:
    path = tmp_path / key.replace("/", "-")
    path.write_bytes(body)
    import hashlib

    return RepositoryObject(key, path, hashlib.sha256(body).hexdigest(), len(body), metadata)


def inventory(tmp_path: Path) -> list[RepositoryObject]:
    values = [
        ("pool/main/d/dkc-linux/a.deb", IMMUTABLE),
        ("dists/trixie/main/binary-amd64/Packages", MUTABLE),
        ("dists/trixie/InRelease", MUTABLE),
        ("state/current.asc", MUTABLE),
        ("manifest.json", MUTABLE),
        ("manifest.json.asc", MUTABLE),
        ("SHA256SUMS", MUTABLE),
        ("SHA256SUMS.asc", MUTABLE),
        ("state/publications/20260817-abcd1234/manifest.json", IMMUTABLE),
        ("state/transactions/20260817-ffff1234/record.json", IMMUTABLE),
        ("state/transactions/20260817-ffff1234/record.json.asc", IMMUTABLE),
    ]
    return [item(tmp_path, key, key.encode(), metadata) for key, metadata in values]


def test_plan_classifies_commit_points_and_captures_each_precondition(tmp_path: Path) -> None:
    store = ConditionalObjectStore()
    old = store.put("dists/trixie/InRelease", b"old", MUTABLE, if_none_match=True)
    plan = plan_repository(inventory(tmp_path), store, max_object_bytes=1_000_000)
    assert plan.inrelease_key not in plan.mutable_before_commit
    assert plan.state_key not in plan.mutable_before_commit
    assert plan.transaction_key in plan.immutable
    assert plan.mutable_preconditions[plan.inrelease_key] == old.etag
    assert plan.mutable_preconditions["state/current.asc"] is None
    assert set(plan.conveniences) == {
        "manifest.json",
        "manifest.json.asc",
        "SHA256SUMS",
        "SHA256SUMS.asc",
    }


def test_plan_rejects_an_oversized_object_before_any_write(tmp_path: Path) -> None:
    store = ConditionalObjectStore()
    with pytest.raises(ValueError, match="size limit"):
        plan_repository(inventory(tmp_path), store, max_object_bytes=10)
    assert store.keys() == ()


def test_plan_rejects_immutable_content_on_a_mutable_path(tmp_path: Path) -> None:
    values = inventory(tmp_path)
    path = tmp_path / "bad"
    path.write_bytes(b"bad")
    import hashlib

    values.append(
        RepositoryObject(
            "keys/bad.gpg", path, hashlib.sha256(b"bad").hexdigest(), 3, IMMUTABLE
        )
    )
    with pytest.raises(ValueError, match="outside allowed"):
        plan_repository(values, ConditionalObjectStore(), max_object_bytes=1_000_000)
