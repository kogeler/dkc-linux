from __future__ import annotations

from datetime import datetime, timezone
import hashlib

import pytest

from dkc.gc import execute_gc, plan_gc
from dkc.records import Artifact, GcQueueEntry, PublicationManifest
from dkc.storage import ObjectMetadata
from tests.fake_storage import ConditionalObjectStore


SHA = "a" * 64
NOW = datetime(2026, 8, 17, tzinfo=timezone.utc)
IMMUTABLE = ObjectMetadata("application/octet-stream", "public, max-age=31536000, immutable")


def manifest(*, digest: str, size: int) -> PublicationManifest:
    live = Artifact("pool/live.deb", SHA, 1, "application/octet-stream", "immutable")
    return PublicationManifest(
        generation=3,
        publication_id="20260817-abcdef12",
        transaction_id="20260817-def67890",
        source_version="7.1.8-2",
        source_dsc_sha256=SHA,
        dkc_version="7.1.8-2+dkc13.1",
        dkc_revision=1,
        build_policy_sha256="c" * 64,
        lto_mode="thin",
        build_id="b" * 12,
        retained_series=[[7, 1]],
        artifacts=[live],
        live_objects=[live.key],
        apt_metadata={"inrelease_sha256": SHA, "date": "x", "valid_until": "y", "index_hashes": {}},
        meta_packages={},
        created_utc="2026-08-17T00:00:00Z",
        previous_publication={"publication_id": "20260816-abcdef12", "generation": 2},
        gc_queue=[GcQueueEntry("pool/retired.deb", digest, size, "retired")],
    )


def test_gc_deletes_only_exact_tombstones() -> None:
    store = ConditionalObjectStore()
    payload = b"retired"
    digest = hashlib.sha256(payload).hexdigest()
    store.put("pool/retired.deb", payload, IMMUTABLE, if_none_match=True)
    plan = plan_gc(manifest(digest=digest, size=len(payload)), store, now=NOW,
                   max_objects=10, max_bytes=1000)
    assert [target.key for target in plan.targets] == ["pool/retired.deb"]
    deleted = execute_gc(plan, store, observed_generation=lambda: 3,
                         mutation_checkpoint=lambda: None)
    assert deleted == ("pool/retired.deb",)
    assert store.get("pool/retired.deb") is None


def test_gc_rejects_changed_bytes() -> None:
    store = ConditionalObjectStore()
    store.put("pool/retired.deb", b"changed", IMMUTABLE, if_none_match=True)
    with pytest.raises(ValueError, match="signed tombstone"):
        plan_gc(manifest(digest=SHA, size=1), store, now=NOW,
                max_objects=10, max_bytes=1000)


def test_gc_refuses_a_partial_plan() -> None:
    store = ConditionalObjectStore()
    first = b"first"
    second = b"second"
    store.put("pool/first.deb", first, IMMUTABLE, if_none_match=True)
    store.put("pool/second.deb", second, IMMUTABLE, if_none_match=True)
    value = manifest(digest=hashlib.sha256(first).hexdigest(), size=len(first))
    value = PublicationManifest(
        **{
            **value.to_dict(),
            "artifacts": value.artifacts,
            "gc_queue": [
                GcQueueEntry("pool/first.deb", hashlib.sha256(first).hexdigest(), len(first), "retired"),
                GcQueueEntry("pool/second.deb", hashlib.sha256(second).hexdigest(), len(second), "retired"),
            ],
        }
    )
    with pytest.raises(ValueError, match="complete GC plan"):
        plan_gc(value, store, now=NOW, max_objects=1, max_bytes=1000)


def test_gc_stops_if_generation_changes() -> None:
    store = ConditionalObjectStore()
    payload = b"retired"
    digest = hashlib.sha256(payload).hexdigest()
    store.put("pool/retired.deb", payload, IMMUTABLE, if_none_match=True)
    plan = plan_gc(manifest(digest=digest, size=len(payload)), store, now=NOW,
                   max_objects=10, max_bytes=1000)
    with pytest.raises(RuntimeError, match="generation changed"):
        execute_gc(plan, store, observed_generation=lambda: 4,
                   mutation_checkpoint=lambda: None)
    assert store.get("pool/retired.deb") is not None
