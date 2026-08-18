"""Convert one verified repository tree into an exact conditional write plan."""

from __future__ import annotations

from collections.abc import Iterable

from .publication import (
    PublicationObject,
    PublicationPlan,
    PublicationStore,
)
from .storage_repository import RepositoryObject

__all__ = ["plan_repository"]


_CONVENIENCES = frozenset(
    {"manifest.json", "manifest.json.asc", "SHA256SUMS", "SHA256SUMS.asc"}
)


def plan_repository(
    inventory: Iterable[RepositoryObject],
    store: PublicationStore,
    *,
    max_object_bytes: int,
) -> PublicationPlan:
    if max_object_bytes < 1:
        raise ValueError("publication object-size limit must be positive")
    objects: dict[str, PublicationObject] = {}
    for item in inventory:
        if item.size > max_object_bytes:
            raise ValueError("a publication object exceeds the configured size limit")
        if item.relative_key in objects:
            raise ValueError("publication inventory repeats an object key")
        objects[item.relative_key] = PublicationObject(item.read(), item.metadata)

    required = {
        "dists/trixie/InRelease",
        "state/current.asc",
        *_CONVENIENCES,
    }
    missing = sorted(required - set(objects))
    if missing:
        raise ValueError(f"publication inventory lacks required objects: {missing}")
    transaction_records = sorted(
        key
        for key in objects
        if key.startswith("state/transactions/") and key.endswith("/record.json")
    )
    if len(transaction_records) != 1:
        raise ValueError("publication inventory must contain one transaction record")
    transaction_key = transaction_records[0]
    if f"{transaction_key}.asc" not in objects:
        raise ValueError("publication transaction record lacks its signature")

    immutable: dict[str, PublicationObject] = {}
    mutable_before: dict[str, PublicationObject] = {}
    conveniences: dict[str, PublicationObject] = {}
    inrelease = objects.pop("dists/trixie/InRelease")
    state_pointer = objects.pop("state/current.asc")
    for key in _CONVENIENCES:
        conveniences[key] = objects.pop(key)
    for key, value in objects.items():
        if value.metadata.cache_control.endswith("immutable"):
            immutable[key] = value
        else:
            mutable_before[key] = value

    for key in immutable:
        if not (
            key.startswith("pool/")
            or "/by-hash/SHA256/" in key
            or key.startswith("state/publications/")
            or key.startswith("state/transactions/")
        ):
            raise ValueError(f"object is classified immutable outside allowed paths: {key}")
    mutable = set(mutable_before) | set(conveniences) | {
        "dists/trixie/InRelease",
        "state/current.asc",
    }
    preconditions = {}
    for key in sorted(mutable):
        current = store.get(key)
        preconditions[key] = None if current is None else current.etag
    return PublicationPlan(
        immutable=immutable,
        mutable_before_commit=mutable_before,
        inrelease=inrelease,
        state_pointer=state_pointer,
        conveniences=conveniences,
        mutable_preconditions=preconditions,
        transaction_key=transaction_key,
    )
