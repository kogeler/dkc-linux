from __future__ import annotations

from typing import cast

import pytest

from dkc.remote_store import S3ObjectStore, validate_namespace
from dkc.s3 import ListedObject, PreconditionFailed, RemoteObject, RemoteStoreError, S3Client
from dkc.storage import ObjectMetadata


METADATA = ObjectMetadata("application/octet-stream", "public, max-age=1")


class FakeClient:
    def __init__(self) -> None:
        self.objects: dict[str, RemoteObject] = {}
        self.raise_after_commit: RemoteStoreError | None = None

    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> str:
        current = self.objects.get(key)
        if if_none_match and current is not None:
            raise PreconditionFailed("put")
        if if_match is not None and (current is None or current.etag != if_match):
            raise PreconditionFailed("put")
        value = RemoteObject(bytes(body), metadata, '"etag-new"')
        self.objects[key] = value
        if self.raise_after_commit is not None:
            error = self.raise_after_commit
            self.raise_after_commit = None
            raise error
        return value.etag

    def get_optional(self, key: str) -> RemoteObject | None:
        return self.objects.get(key)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def list_keys(
        self, prefix: str, *, page_size: int | None = None
    ) -> tuple[str, ...]:
        del page_size
        return tuple(sorted(key for key in self.objects if key.startswith(prefix)))

    def list_objects(
        self, prefix: str, *, page_size: int | None = None
    ) -> tuple[ListedObject, ...]:
        del page_size
        return tuple(
            ListedObject(key, len(value.body))
            for key, value in sorted(self.objects.items())
            if key.startswith(prefix)
        )


def _store(client: FakeClient, namespace: str = "test/root/") -> S3ObjectStore:
    return S3ObjectStore(cast(S3Client, client), namespace)


def test_lost_success_response_is_reconciled_by_exact_authoritative_read() -> None:
    client = FakeClient()
    client.raise_after_commit = RemoteStoreError("PutObject", 0, "connection lost")
    result = _store(client).put(
        "pool/package.deb", b"payload", METADATA, if_none_match=True
    )
    assert result.body == b"payload"
    assert result.metadata == METADATA


def test_lost_response_with_different_remote_bytes_stays_failed() -> None:
    class CorruptingClient(FakeClient):
        def get_optional(self, key: str) -> RemoteObject | None:
            current = super().get_optional(key)
            if current is None:
                return None
            return RemoteObject(b"different", current.metadata, current.etag)

    client = CorruptingClient()
    client.raise_after_commit = RemoteStoreError("PutObject", 503, "unavailable")
    with pytest.raises(RemoteStoreError, match="503"):
        _store(client).put(
            "pool/package.deb", b"payload", METADATA, if_none_match=True
        )


def test_412_is_never_relabelled_as_success() -> None:
    client = FakeClient()
    client.objects["test/root/state/current.asc"] = RemoteObject(
        b"winner", METADATA, '"winner"'
    )
    with pytest.raises(PreconditionFailed):
        _store(client).put(
            "state/current.asc", b"winner", METADATA, if_match='"stale"'
        )


def test_immutable_reuse_requires_exact_bytes_and_metadata() -> None:
    client = FakeClient()
    store = _store(client)
    store.create_immutable("pool/x", b"same", METADATA)
    store.create_immutable("pool/x", b"same", METADATA)
    with pytest.raises(PreconditionFailed, match="collision"):
        store.create_immutable("pool/x", b"different", METADATA)


def test_namespace_is_validated_and_listing_cannot_escape() -> None:
    with pytest.raises(ValueError, match="namespace"):
        validate_namespace("../production/")
    client = FakeClient()
    store = _store(client)
    store.create_immutable("pool/a", b"a", METADATA)
    client.objects["outside/object"] = RemoteObject(b"x", METADATA, '"x"')
    assert store.list_keys("pool/") == ("pool/a",)
    assert store.list_objects("pool/") == (ListedObject("pool/a", 1),)
