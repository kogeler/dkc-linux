"""Validated repository inventory and disposable object-key construction."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

from .schema import validate as validate_schema
from .storage import ObjectMetadata

__all__ = [
    "RepositoryObject",
    "build_disposable_prefix",
    "load_verified_repository",
    "validate_disposable_prefix",
]


IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
MUTABLE_CACHE = "public, max-age=0, must-revalidate"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_NAMESPACE_RE = re.compile(r"^[0-9a-f]{24}$")
_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class RepositoryObject:
    relative_key: str
    path: Path
    sha256: str
    size: int
    metadata: ObjectMetadata

    def read(self) -> bytes:
        body = self.path.read_bytes()
        if len(body) != self.size or hashlib.sha256(body).hexdigest() != self.sha256:
            raise RuntimeError(f"repository object changed after inventory: {self.relative_key}")
        return body


def _relative_key(raw: str) -> str:
    parts = raw.split("/")
    if (
        not raw
        or raw != raw.strip()
        or raw.startswith("/")
        or raw.endswith("/")
        or "\\" in raw
        or "*" in raw
        or _CONTROL_RE.search(raw)
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ValueError(f"unsafe repository path: {raw!r}")
    return "/".join(parts)


def _checksum_lines(path: Path, *, leading_dot: bool) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        if match is None:
            raise ValueError(f"malformed checksum line {line_number} in {path.name}")
        raw_key = match.group(2)
        if leading_dot:
            if not raw_key.startswith("./"):
                raise ValueError(f"evidence checksum path lacks ./ prefix: {raw_key!r}")
            raw_key = raw_key[2:]
        key = _relative_key(raw_key)
        if key in result:
            raise ValueError(f"duplicate checksum path: {key}")
        result[key] = match.group(1)
    if not result:
        raise ValueError(f"empty checksum inventory: {path}")
    return result


def _all_regular_files(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"repository result contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"repository result contains a non-regular file: {path}")
        key = _relative_key(path.relative_to(root).as_posix())
        result[key] = path
    return result


def _verify_checksums(
    root: Path,
    checksums: dict[str, str],
    *,
    excluded: frozenset[str] = frozenset(),
) -> None:
    files = _all_regular_files(root)
    expected = set(files) - set(excluded)
    if set(checksums) != expected:
        missing = sorted(expected - set(checksums))
        extra = sorted(set(checksums) - expected)
        raise ValueError(f"checksum inventory mismatch: missing={missing} extra={extra}")
    for key, digest in checksums.items():
        actual = hashlib.sha256(files[key].read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError(f"checksum mismatch for {key}")


def _result_status(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        if not re.fullmatch(r"[a-z0-9_]+=[A-Za-z0-9._-]+", line):
            raise ValueError(f"unsafe repository result status line: {line!r}")
        key, value = line.split("=", 1)
        if key in values:
            raise ValueError(f"duplicate repository result status: {key}")
        values[key] = value
    required = {
        "status": "PASS",
        "repository_assembly": "PASS",
        "repository_signing": "PASS",
        "signature_handoff": "PASS",
        "signed_apt_client": "PASS",
        "source_packages": "PASS",
        "by_hash": "PASS",
        "publishable": "false",
    }
    for key, value in required.items():
        if values.get(key) != value:
            raise ValueError(f"verified repository gate {key} is not {value}")
    return values


def _derived_metadata(key: str) -> ObjectMetadata:
    immutable = key.startswith("state/publications/") or key.startswith(
        "state/transactions/"
    )
    if key.endswith(".json"):
        content_type = "application/json"
    elif key.endswith(".asc"):
        content_type = "application/pgp-signature"
    elif key == "SHA256SUMS":
        content_type = "text/plain"
    else:
        guessed, _ = mimetypes.guess_type(key)
        content_type = guessed or "application/octet-stream"
    return ObjectMetadata(
        content_type=content_type,
        cache_control=IMMUTABLE_CACHE if immutable else MUTABLE_CACHE,
    )


def load_verified_repository(result: Path) -> tuple[RepositoryObject, ...]:
    """Load the exact repository tree only after all prior evidence verifies."""

    root = result.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("verified repository result is not a directory")
    evidence = root / "evidence"
    repository = root / "repository"
    if not evidence.is_dir() or not repository.is_dir():
        raise ValueError("verified repository result lacks evidence or repository")
    _result_status(evidence / "result.env")
    evidence_sums = _checksum_lines(
        evidence / "evidence.sha256", leading_dot=True
    )
    _verify_checksums(
        root,
        evidence_sums,
        excluded=frozenset({"evidence/evidence.sha256"}),
    )

    repository_sums = _checksum_lines(repository / "SHA256SUMS", leading_dot=False)
    _verify_checksums(
        repository,
        repository_sums,
        excluded=frozenset({"SHA256SUMS", "SHA256SUMS.asc"}),
    )

    manifest_path = repository / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("repository manifest is invalid JSON") from exc
    validate_schema("publication-manifest", manifest)
    if not isinstance(manifest, dict) or manifest.get("schema") != "dkc.publication-manifest.v1":
        raise ValueError("repository manifest has the wrong schema")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise ValueError("repository manifest lacks an artifact inventory")

    metadata_by_key: dict[str, ObjectMetadata] = {}
    for raw in artifacts:
        if not isinstance(raw, dict):
            raise ValueError("repository manifest artifact is not an object")
        try:
            raw_key = raw["key"]
            raw_digest = raw["sha256"]
            raw_size = raw["size"]
            raw_content_type = raw["media_type"]
            raw_cache_class = raw["cache_class"]
        except KeyError as exc:
            raise ValueError("repository manifest artifact is malformed") from exc
        if (
            not isinstance(raw_key, str)
            or not isinstance(raw_digest, str)
            or not isinstance(raw_size, int)
            or isinstance(raw_size, bool)
            or not isinstance(raw_content_type, str)
            or not isinstance(raw_cache_class, str)
        ):
            raise ValueError("repository manifest artifact has the wrong field types")
        key = _relative_key(raw_key)
        digest = raw_digest
        size = raw_size
        content_type = raw_content_type
        cache_class = raw_cache_class
        if key in metadata_by_key:
            raise ValueError(f"duplicate repository manifest artifact: {key}")
        if not _SHA256_RE.fullmatch(digest) or size < 0:
            raise ValueError(f"invalid repository manifest identity for {key}")
        if repository_sums.get(key) != digest:
            raise ValueError(f"manifest/checksum mismatch for {key}")
        path = repository / key
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError(f"manifest size mismatch for {key}")
        if not content_type or _CONTROL_RE.search(content_type):
            raise ValueError(f"unsafe manifest media type for {key}")
        if cache_class not in ("immutable", "mutable"):
            raise ValueError(f"unsafe manifest cache class for {key}")
        metadata_by_key[key] = ObjectMetadata(
            content_type,
            IMMUTABLE_CACHE if cache_class == "immutable" else MUTABLE_CACHE,
        )

    manifest_keys = set(metadata_by_key)
    allowed_derived = {
        "manifest.json",
        "manifest.json.asc",
        "SHA256SUMS",
        "SHA256SUMS.asc",
        "state/current.asc",
    }
    allowed_derived.update(
        key
        for key in repository_sums
        if key.startswith("state/publications/")
        or key.startswith("state/transactions/")
    )
    repository_files = set(_all_regular_files(repository))
    if repository_files != manifest_keys | allowed_derived:
        missing = sorted(repository_files - manifest_keys - allowed_derived)
        absent = sorted((manifest_keys | allowed_derived) - repository_files)
        raise ValueError(
            f"repository manifest boundary mismatch: unclassified={missing} absent={absent}"
        )

    objects: list[RepositoryObject] = []
    for key in sorted(repository_files):
        path = repository / key
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if key in repository_sums and repository_sums[key] != digest:
            raise ValueError(f"repository changed while inventorying {key}")
        objects.append(
            RepositoryObject(
                relative_key=key,
                path=path,
                sha256=digest,
                size=path.stat().st_size,
                metadata=metadata_by_key.get(key, _derived_metadata(key)),
            )
        )
    return tuple(objects)


def build_disposable_prefix(canonical_repository: str, run_id: str, nonce: str) -> str:
    if not _REPOSITORY_RE.fullmatch(canonical_repository):
        raise ValueError("canonical repository must be owner/repo")
    if not _RUN_RE.fullmatch(run_id):
        raise ValueError("unsafe disposable run ID")
    if not _NONCE_RE.fullmatch(nonce):
        raise ValueError("disposable nonce must be 16 random bytes in lowercase hex")
    namespace = hashlib.sha256(canonical_repository.encode("utf-8")).hexdigest()[:24]
    prefix = f"_dkc-test/storage/{namespace}/{run_id}-{nonce}/"
    if not prefix.startswith("_dkc-test/storage/") or f"-{nonce}/" not in prefix:
        raise AssertionError("disposable prefix construction lost its safety marker")
    return prefix


def validate_disposable_prefix(prefix: str) -> str:
    marker = "_dkc-test/storage/"
    if not prefix.startswith(marker) or not prefix.endswith("/"):
        raise ValueError("unsafe disposable storage prefix")
    suffix = prefix[len(marker) : -1]
    parts = suffix.split("/")
    if len(parts) != 2:
        raise ValueError("unsafe disposable storage prefix")
    namespace, run_nonce = parts
    if not _NAMESPACE_RE.fullmatch(namespace):
        raise ValueError("unsafe disposable storage prefix")
    if "-" not in run_nonce:
        raise ValueError("unsafe disposable storage prefix")
    run_id, nonce = run_nonce.rsplit("-", 1)
    if f"_dkc-test/storage/{namespace}/{run_id}-{nonce}/" != prefix:
        raise ValueError("unsafe disposable storage prefix")
    return prefix
