from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dkc.lease import LeaseBusy, LeaseManager
from dkc.records import LeaseOwner
from dkc.serialize import canonical_bytes
from dkc.site import (
    CONTENT_TYPES,
    SiteError,
    SiteManifest,
    SiteObject,
    manifest_document,
)
from dkc.site_publication import (
    acquire_lease,
    execute_site_plan,
    load_site_build,
    plan_site_publication,
    verify_site_publication,
)
from dkc.storage import ObjectMetadata
from tests.fake_storage import ConditionalObjectStore

REVISION = "0123456789abcdef0123456789abcdef01234567"
SITE_URL = "https://site.example.net/"
FOREIGN = ObjectMetadata("text/plain", "public, max-age=0, must-revalidate")


def _result(root: Path, files: dict[str, bytes]) -> Path:
    site = root / "site"
    objects = []
    for key, body in sorted(files.items()):
        path = site / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        objects.append(
            SiteObject(
                key=key,
                sha256=hashlib.sha256(body).hexdigest(),
                size=len(body),
                content_type=CONTENT_TYPES[Path(key).suffix],
            )
        )
    manifest = manifest_document(SiteManifest(SITE_URL, REVISION, tuple(objects)))
    (root / "site-manifest.json").write_bytes(canonical_bytes(manifest))
    (root / "summary.json").write_bytes(canonical_bytes({"status": "PASS"}))
    records = [
        f"{hashlib.sha256((root / name).read_bytes()).hexdigest()}  {name}"
        for name in ("site-manifest.json", "summary.json")
    ]
    (root / "evidence.sha256").write_text("\n".join(records) + "\n", encoding="utf-8")
    return root


FILES = {
    "index.html": b"<h1>home</h1>",
    "docs/a.html": b"<h1>a</h1>",
    "assets/site.css": b"body{}",
}


def _publish(build_root: Path, store: ConditionalObjectStore) -> list[str]:
    build = load_site_build(build_root)
    plan = plan_site_publication(build, store, max_deletions=10)
    checkpoints: list[str] = []
    execute_site_plan(plan, store, mutation_checkpoint=lambda: checkpoints.append("x"))
    verify_site_publication(build, store)
    return checkpoints


def test_a_first_publication_creates_every_object_assets_first(tmp_path: Path) -> None:
    store = ConditionalObjectStore()
    store.put("dists/trixie/InRelease", b"signed", FOREIGN, if_none_match=True)
    build = load_site_build(_result(tmp_path / "one", FILES))
    plan = plan_site_publication(build, store, max_deletions=10)
    assert [write.key for write in plan.writes] == [
        "site/assets/site.css",
        "site/docs/a.html",
        "site/index.html",
    ]
    assert all(write.if_match is None for write in plan.writes)
    checkpoints: list[str] = []
    execute_site_plan(plan, store, mutation_checkpoint=lambda: checkpoints.append("x"))
    verify_site_publication(build, store)
    assert len(checkpoints) == 3
    value = store.get("site/index.html")
    assert value is not None
    assert value.metadata == ObjectMetadata(
        "text/html; charset=utf-8", "public, max-age=0, must-revalidate"
    )
    assert store.get("dists/trixie/InRelease") is not None


def test_an_unchanged_build_writes_nothing(tmp_path: Path) -> None:
    store = ConditionalObjectStore()
    _publish(_result(tmp_path / "one", FILES), store)
    build = load_site_build(_result(tmp_path / "two", FILES))
    plan = plan_site_publication(build, store, max_deletions=10)
    assert plan.writes == () and plan.deletions == ()
    assert len(plan.unchanged) == 3


def test_changes_use_preconditions_and_deletions_stay_in_the_prefix(
    tmp_path: Path,
) -> None:
    store = ConditionalObjectStore()
    _publish(_result(tmp_path / "one", FILES), store)
    store.put("state/current.asc", b"state", FOREIGN, if_none_match=True)
    store.put("sitemap.txt", b"root", FOREIGN, if_none_match=True)
    changed = {"index.html": b"<h1>new</h1>", "assets/site.css": b"body{}"}
    build = load_site_build(_result(tmp_path / "two", changed))
    plan = plan_site_publication(build, store, max_deletions=10)
    current = store.get("site/index.html")
    assert current is not None
    assert [(write.key, write.if_match) for write in plan.writes] == [
        ("site/index.html", current.etag)
    ]
    assert plan.deletions == ("site/docs/a.html",)
    execute_site_plan(plan, store, mutation_checkpoint=lambda: None)
    verify_site_publication(build, store)
    assert store.keys() == (
        "site/assets/site.css",
        "site/index.html",
        "sitemap.txt",
        "state/current.asc",
    )


def test_the_deletion_cap_fails_before_any_mutation(tmp_path: Path) -> None:
    store = ConditionalObjectStore()
    _publish(_result(tmp_path / "one", FILES), store)
    before = store.keys()
    build = load_site_build(_result(tmp_path / "two", {"index.html": b"<h1>x</h1>"}))
    with pytest.raises(SiteError, match="safety cap"):
        plan_site_publication(build, store, max_deletions=1)
    assert store.keys() == before


def test_a_failed_checkpoint_stops_mutation(tmp_path: Path) -> None:
    store = ConditionalObjectStore()
    build = load_site_build(_result(tmp_path / "one", FILES))
    plan = plan_site_publication(build, store, max_deletions=10)
    calls: list[int] = []

    def checkpoint() -> None:
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        execute_site_plan(plan, store, mutation_checkpoint=checkpoint)
    assert store.keys() == ("site/assets/site.css",)


def test_verification_detects_a_changed_or_extra_object(tmp_path: Path) -> None:
    store = ConditionalObjectStore()
    root = _result(tmp_path / "one", FILES)
    _publish(root, store)
    build = load_site_build(root)
    current = store.get("site/docs/a.html")
    assert current is not None
    store.put("site/docs/a.html", b"tampered", current.metadata, if_match=current.etag)
    with pytest.raises(SiteError, match="differs from the build"):
        verify_site_publication(build, store)
    _publish(root, store)
    store.put("site/extra.html", b"x", FOREIGN, if_none_match=True)
    with pytest.raises(SiteError, match="listing differs"):
        verify_site_publication(build, store)


def test_a_modified_build_result_is_rejected_before_any_request(tmp_path: Path) -> None:
    root = _result(tmp_path / "one", FILES)
    (root / "site/docs/a.html").write_bytes(b"changed")
    with pytest.raises(SiteError, match="differs from its manifest"):
        load_site_build(root)

    root = _result(tmp_path / "two", FILES)
    (root / "site/extra.html").write_bytes(b"x")
    with pytest.raises(SiteError, match="tree differs"):
        load_site_build(root)

    root = _result(tmp_path / "three", FILES)
    (root / "summary.json").write_bytes(b"{}\n")
    with pytest.raises(SiteError, match="evidence"):
        load_site_build(root)

    root = _result(tmp_path / "four", FILES)
    (root / "site/link.html").symlink_to(root / "site/index.html")
    with pytest.raises(SiteError, match="symlink"):
        load_site_build(root)


def _owner(nonce: str) -> LeaseOwner:
    return LeaseOwner("kogeler/dkc-linux", "7", "1", "site-publish", nonce)


def test_lease_acquisition_waits_for_a_busy_holder() -> None:
    store = ConditionalObjectStore()
    manager = LeaseManager(
        store,
        ttl_seconds=900,
        takeover_grace_seconds=300,
        terminal_proof=lambda _owner: False,
        clock=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
    )
    holder = manager.acquire(
        LeaseOwner("kogeler/dkc-linux", "6", "1", "publish", "b" * 32)
    )
    now = [0.0]
    waits: list[int] = []

    def sleep(seconds: float) -> None:
        now[0] += seconds
        manager.release(holder)

    handle = acquire_lease(
        manager,
        _owner("a" * 32),
        wait_seconds=60,
        poll_seconds=30,
        sleep=sleep,
        clock=lambda: now[0],
        on_wait=lambda: waits.append(1),
    )
    assert handle.owner.operation == "site-publish"
    assert waits == [1]


def test_lease_acquisition_gives_up_after_its_bound() -> None:
    store = ConditionalObjectStore()
    manager = LeaseManager(
        store,
        ttl_seconds=900,
        takeover_grace_seconds=300,
        terminal_proof=lambda _owner: False,
        clock=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
    )
    manager.acquire(LeaseOwner("kogeler/dkc-linux", "6", "1", "publish", "b" * 32))
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(LeaseBusy):
        acquire_lease(
            manager,
            _owner("a" * 32),
            wait_seconds=60,
            poll_seconds=30,
            sleep=sleep,
            clock=lambda: now[0],
        )
    assert now[0] == 60
