"""Conditional publication of one audited site build below the site/ prefix."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .lease import LeaseBusy, LeaseHandle, LeaseManager
from .records import LeaseOwner
from .serialize import loads
from .site import SiteError, SiteManifest, SiteObject, parse_manifest
from .storage import ObjectMetadata

__all__ = [
    "SITE_PREFIX",
    "SiteBuild",
    "SitePlan",
    "SiteWrite",
    "acquire_lease",
    "execute_site_plan",
    "load_site_build",
    "plan_site_publication",
    "verify_site_publication",
]

# The only storage namespace this publisher may write or delete.
SITE_PREFIX = "site/"

_EVIDENCE_FILES = frozenset({"site-manifest.json", "summary.json"})
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class SiteValue(Protocol):
    @property
    def body(self) -> bytes: ...

    @property
    def metadata(self) -> ObjectMetadata: ...

    @property
    def etag(self) -> str: ...


class SiteStore(Protocol):
    def get(self, key: str) -> SiteValue | None: ...

    def put(
        self,
        key: str,
        body: bytes,
        metadata: ObjectMetadata,
        *,
        if_none_match: bool = False,
        if_match: str | None = None,
    ) -> SiteValue: ...

    def delete(self, key: str) -> None: ...

    def list_keys(self, prefix: str) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class SiteBuild:
    manifest: SiteManifest
    bodies: Mapping[str, bytes]


def _metadata(item: SiteObject) -> ObjectMetadata:
    return ObjectMetadata(item.content_type, item.cache_control)


def load_site_build(result: Path) -> SiteBuild:
    """Verify one site build result completely before any remote request."""

    evidence: dict[str, str] = {}
    for line in (result / "evidence.sha256").read_text(encoding="utf-8").splitlines():
        digest, separator, name = line.partition("  ")
        if not separator or not _SHA256_RE.fullmatch(digest) or name in evidence:
            raise SiteError("site build evidence is malformed")
        evidence[name] = digest
    if set(evidence) != _EVIDENCE_FILES:
        raise SiteError("site build evidence covers an unexpected file set")
    for name, digest in evidence.items():
        path = result / name
        if path.is_symlink() or not path.is_file():
            raise SiteError(f"site build record is not a regular file: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SiteError(f"site build record does not match its evidence: {name}")
    manifest = parse_manifest(loads((result / "site-manifest.json").read_bytes()))

    root = result / "site"
    if root.is_symlink() or not root.is_dir():
        raise SiteError("site build tree is missing")
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SiteError("site build tree contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir():
            raise SiteError("site build tree contains a special file")
    if actual != {item.key for item in manifest.objects}:
        raise SiteError("site build tree differs from its manifest")
    bodies: dict[str, bytes] = {}
    for item in manifest.objects:
        body = (root / item.key).read_bytes()
        if len(body) != item.size or hashlib.sha256(body).hexdigest() != item.sha256:
            raise SiteError(f"site object differs from its manifest: {item.key}")
        bodies[item.key] = body
    return SiteBuild(manifest, bodies)


@dataclass(frozen=True)
class SiteWrite:
    key: str
    body: bytes
    metadata: ObjectMetadata
    if_match: str | None


@dataclass(frozen=True)
class SitePlan:
    writes: tuple[SiteWrite, ...]
    unchanged: tuple[str, ...]
    deletions: tuple[str, ...]


def _write_order(key: str) -> tuple[int, str]:
    # Assets first, then pages, then the home page, so a new page never names
    # an asset that is not yet present.
    name = key[len(SITE_PREFIX) :]
    if name == "index.html":
        return (2, key)
    return (1 if name.endswith(".html") else 0, key)


def _desired(build: SiteBuild) -> dict[str, SiteObject]:
    return {f"{SITE_PREFIX}{item.key}": item for item in build.manifest.objects}


def _listed(store: SiteStore) -> set[str]:
    keys = set(store.list_keys(SITE_PREFIX))
    if any(not key.startswith(SITE_PREFIX) for key in keys):
        raise SiteError("storage listing escaped the site prefix")
    return keys


def plan_site_publication(
    build: SiteBuild, store: SiteStore, *, max_deletions: int
) -> SitePlan:
    """Compare the build with the current site prefix exactly."""

    if max_deletions < 0:
        raise ValueError("site deletion cap must not be negative")
    listed = _listed(store)
    desired = _desired(build)
    writes: list[SiteWrite] = []
    unchanged: list[str] = []
    for key in sorted(desired, key=_write_order):
        item = desired[key]
        body = build.bodies[item.key]
        metadata = _metadata(item)
        current = store.get(key)
        if (
            current is not None
            and current.body == body
            and current.metadata == metadata
        ):
            unchanged.append(key)
            continue
        writes.append(
            SiteWrite(key, body, metadata, None if current is None else current.etag)
        )
    deletions = tuple(sorted(listed - set(desired)))
    if len(deletions) > max_deletions:
        raise SiteError("site deletion plan exceeds its safety cap")
    return SitePlan(tuple(writes), tuple(unchanged), deletions)


def execute_site_plan(
    plan: SitePlan,
    store: SiteStore,
    *,
    mutation_checkpoint: Callable[[], None],
) -> None:
    """Apply every conditional write, then every exact deletion."""

    for write in plan.writes:
        if not write.key.startswith(SITE_PREFIX):
            raise SiteError("site write escaped the site prefix")
        mutation_checkpoint()
        if write.if_match is None:
            store.put(write.key, write.body, write.metadata, if_none_match=True)
        else:
            store.put(write.key, write.body, write.metadata, if_match=write.if_match)
    for key in plan.deletions:
        if not key.startswith(SITE_PREFIX):
            raise SiteError("site deletion escaped the site prefix")
        mutation_checkpoint()
        store.delete(key)


def verify_site_publication(build: SiteBuild, store: SiteStore) -> None:
    """Require the published prefix to equal the build byte for byte."""

    desired = _desired(build)
    if _listed(store) != set(desired):
        raise SiteError("published site listing differs from the build")
    for key, item in desired.items():
        current = store.get(key)
        if (
            current is None
            or current.body != build.bodies[item.key]
            or current.metadata != _metadata(item)
        ):
            raise SiteError(f"published site object differs from the build: {key}")


def acquire_lease(
    manager: LeaseManager,
    owner: LeaseOwner,
    *,
    wait_seconds: int,
    poll_seconds: int = 30,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_wait: Callable[[], None] | None = None,
) -> LeaseHandle:
    """Acquire the production lease, waiting a bounded time while it is busy."""

    if wait_seconds < 0 or poll_seconds < 1:
        raise ValueError("lease wait bounds are invalid")
    deadline = clock() + wait_seconds
    while True:
        try:
            return manager.acquire(owner)
        except LeaseBusy:
            if clock() + poll_seconds > deadline:
                raise
            if on_wait is not None:
                on_wait()
            sleep(poll_seconds)
