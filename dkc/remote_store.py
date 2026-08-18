"""Conditional object-store adapter over the provider-neutral S3 client."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .s3 import ListedObject, PreconditionFailed, RemoteObject, RemoteStoreError, S3Client
from .storage import ObjectMetadata

__all__ = ["S3ObjectStore", "validate_namespace"]


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def validate_namespace(value: str) -> str:
    if not value:
        return value
    parts = value.split("/")
    if (
        not value.endswith("/")
        or value.startswith("/")
        or "*" in value
        or "\\" in value
        or _CONTROL_RE.search(value)
        or any(part in ("", ".", "..") for part in parts[:-1])
        or parts[-1] != ""
    ):
        raise ValueError("unsafe object-store namespace")
    return value


@dataclass(frozen=True, repr=False)
class S3ObjectStore:
    """Expose logical keys below one validated namespace.

    A lost response is ambiguous: the conditional request may have committed.
    Reconciliation therefore performs an authoritative read and accepts only
    exact intended bytes plus metadata. It never retries unconditionally.
    """

    client: S3Client
    namespace: str = ""

    def __post_init__(self) -> None:
        validate_namespace(self.namespace)

    def _key(self, key: str) -> str:
        if not key or key.startswith("/") or key.endswith("/"):
            raise ValueError("unsafe logical object key")
        return f"{self.namespace}{key}"

    def _strip(self, key: str) -> str:
        if not key.startswith(self.namespace):
            raise RuntimeError("object listing escaped its namespace")
        return key[len(self.namespace) :]

    def get(self, key: str) -> RemoteObject | None:
        return self.client.get_optional(self._key(key))

    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> RemoteObject:
        remote_key = self._key(key)
        try:
            etag = self.client.put(
                remote_key,
                body,
                metadata,
                if_none_match=if_none_match,
                if_match=if_match,
            )
            return RemoteObject(bytes(body), metadata, etag)
        except RemoteStoreError as exc:
            # HTTP 412 is an unambiguous lost precondition and must never be
            # reconciled into success merely because another writer happened
            # to publish equal bytes.
            if isinstance(exc, PreconditionFailed) or exc.status not in (
                0,
                500,
                502,
                503,
                504,
            ):
                raise
            current = self.client.get_optional(remote_key)
            if current is not None and current.body == body and current.metadata == metadata:
                return current
            raise

    def create_immutable(
        self, key: str, body: bytes, metadata: ObjectMetadata
    ) -> RemoteObject:
        try:
            return self.put(key, body, metadata, if_none_match=True)
        except PreconditionFailed:
            current = self.get(key)
            if current is None or current.body != body or current.metadata != metadata:
                raise PreconditionFailed(f"immutable object collision for {key}") from None
            return current

    def delete(self, key: str) -> None:
        remote_key = self._key(key)
        try:
            self.client.delete(remote_key)
        except RemoteStoreError as exc:
            if exc.status not in (0, 500, 502, 503, 504):
                raise
            if self.client.get_optional(remote_key) is None:
                return
            raise

    def list_keys(
        self, prefix: str, *, page_size: int | None = None
    ) -> tuple[str, ...]:
        remote_prefix = f"{self.namespace}{prefix}"
        return tuple(
            self._strip(key)
            for key in self.client.list_keys(remote_prefix, page_size=page_size)
        )

    def list_objects(
        self, prefix: str, *, page_size: int | None = None
    ) -> tuple[ListedObject, ...]:
        remote_prefix = f"{self.namespace}{prefix}"
        return tuple(
            ListedObject(self._strip(item.key), item.size)
            for item in self.client.list_objects(remote_prefix, page_size=page_size)
        )
