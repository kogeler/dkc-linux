"""Bounded, self-verifying evidence bundles for GitHub workflow artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .evidence import verify_evidence_directory
from .serialize import dumps

__all__ = [
    "prepare_flavor_evidence",
    "prepare_pull_request_repository_evidence",
]


_BUNDLE_SCHEMA = "dkc.github-evidence.v1"
_MAX_FILES = 512
_MAX_FILE_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_RESERVED_NAMES = frozenset(("bundle.json", "evidence.sha256"))


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if (
        not value
        or value != value.strip()
        or path.as_posix() != value
        or path.is_absolute()
        or "\\" in value
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("artifact evidence contains an unsafe destination")
    if path.parts[0] in _RESERVED_NAMES:
        raise ValueError("artifact evidence uses a reserved destination")
    return path


def _require_plain_directory(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} is a symbolic link")
    resolved = path.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError(f"{label} is not a directory")
    return resolved


def _tree_files(
    root: Path,
    destination: str,
    *,
    omit_names: frozenset[str] = frozenset(),
    omit_suffixes: tuple[str, ...] = (),
) -> dict[str, Path]:
    resolved = _require_plain_directory(root, destination)
    files: dict[str, Path] = {}
    prefix = _safe_relative(destination)
    for path in sorted(resolved.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"{destination} contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"{destination} contains a special file")
        if path.name in omit_names or path.name.endswith(omit_suffixes):
            continue
        relative = path.relative_to(resolved)
        target = (prefix / relative).as_posix()
        files[target] = path
    return files


def _selected_files(
    root: Path,
    names: Mapping[str, str],
    *,
    label: str,
) -> dict[str, Path]:
    resolved = _require_plain_directory(root, label)
    files: dict[str, Path] = {}
    for destination, name in sorted(names.items()):
        target = _safe_relative(destination).as_posix()
        relative = _safe_relative(name)
        source = resolved / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"{label} lacks bounded evidence: {name}")
        files[target] = source
    return files


def _cache_payload_roots(cache: Path, manifest: Mapping[str, Any]) -> dict[str, Path]:
    payload = manifest.get("payload")
    expected = ("flavor", "kselftest", "qemu")
    if not isinstance(payload, dict) or set(payload) != set(expected):
        raise ValueError("release-cache manifest has an invalid payload map")
    roots: dict[str, Path] = {}
    observed: set[Path] = set()
    for name in expected:
        value = payload.get(name)
        if not isinstance(value, str):
            raise ValueError("release-cache manifest has an invalid payload path")
        relative = _safe_relative(value)
        if len(relative.parts) != 1 or relative in observed:
            raise ValueError("release-cache manifest has an invalid payload path")
        observed.add(relative)
        roots[name] = _require_plain_directory(
            cache / relative, f"release-cache {name} payload"
        )
    return roots


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_inventory(root: Path) -> None:
    records: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("artifact evidence contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("artifact evidence contains a special file")
        relative = path.relative_to(root).as_posix()
        if relative != "evidence.sha256":
            records.append(f"{_sha256(path)}  {relative}")
    if not records:
        raise ValueError("artifact evidence is empty")
    (root / "evidence.sha256").write_text(
        "\n".join(records) + "\n", encoding="utf-8"
    )


def _copy_bounded(files: Mapping[str, Path], target: Path) -> None:
    if not files or len(files) > _MAX_FILES:
        raise ValueError("artifact evidence has an invalid file count")
    total = 0
    for raw_destination, source in sorted(files.items()):
        destination = _safe_relative(raw_destination)
        if source.is_symlink() or not source.is_file():
            raise ValueError("artifact evidence source is not a regular file")
        size = source.stat().st_size
        if size > _MAX_FILE_BYTES:
            raise ValueError("artifact evidence file exceeds its size limit")
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise ValueError("artifact evidence exceeds its total size limit")
        output = target / destination
        output.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as source_stream, output.open("xb") as output_stream:
            shutil.copyfileobj(source_stream, output_stream, length=1024 * 1024)
        output.chmod(0o644)


def _prepare_bundle(
    output: Path,
    *,
    files: Mapping[str, Path],
    metadata: Mapping[str, Any],
) -> Path:
    output = Path(os.path.abspath(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.parent.is_symlink():
        raise ValueError("artifact evidence parent is a symbolic link")
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        _copy_bounded(files, temporary)
        document = {"schema": _BUNDLE_SCHEMA, **metadata}
        (temporary / "bundle.json").write_text(dumps(document), encoding="utf-8")
        _write_inventory(temporary)
        verify_evidence_directory(temporary)
        if output.exists() or output.is_symlink():
            if output.is_symlink() or not output.is_dir():
                raise ValueError("artifact evidence output is not a plain directory")
            verify_evidence_directory(output)
            if (output / "evidence.sha256").read_bytes() != (
                temporary / "evidence.sha256"
            ).read_bytes():
                raise ValueError("existing artifact evidence differs")
            return output
        os.replace(temporary, output)
        return output
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _read_environment(path: Path, label: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{label} is not readable text") from exc
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in values or "\x00" in value:
            raise ValueError(f"{label} is malformed")
        values[key] = value
    return values


def prepare_flavor_evidence(
    cache_root: Path,
    output: Path,
    *,
    flavor: str,
) -> Path:
    """Export compact build, selftest, and VM reports from an accepted cache."""

    if flavor not in ("v2", "v3"):
        raise ValueError("artifact evidence is limited to release flavors")
    cache = _require_plain_directory(cache_root, "release cache")
    manifest = _read_json_object(cache / "cache.json", "release-cache manifest")
    identity = manifest.get("identity")
    if (
        manifest.get("schema") != "dkc.release-cache.v2"
        or not isinstance(identity, dict)
        or identity.get("flavor") != flavor
    ):
        raise ValueError("release-cache manifest has the wrong flavor identity")
    payload = _cache_payload_roots(cache, manifest)

    files = _selected_files(
        cache, {"cache.json": "cache.json"}, label="release cache"
    )
    files.update(
        _selected_files(
            payload["flavor"],
            {
                f"flavor/evidence/{name}": f"evidence/{name}"
                for name in (
                    "result.env",
                    "publication-identity.json",
                    "build-image-provenance.env",
                    "post-build-gates.env",
                    "capacity.env",
                    "attestation.json",
                    "kbuild-command-audit.json",
                    "kernel-simd-audit.json",
                )
            },
            label="release-cache flavor payload",
        )
    )
    files.update(
        _selected_files(
            payload["kselftest"],
            {
                "selftest/evidence/result.env": "evidence/result.env",
                "selftest/evidence/kselftest-build.json": (
                    "evidence/kselftest-build.json"
                ),
            },
            label="release-cache kselftest payload",
        )
    )
    omitted = frozenset(("evidence.sha256",))
    files.update(
        _tree_files(
            payload["qemu"] / "evidence",
            "qemu/evidence",
            omit_names=omitted,
        )
    )
    files.update(
        _tree_files(
            payload["qemu"] / flavor / "evidence",
            f"qemu/{flavor}/evidence",
            omit_names=omitted,
        )
    )
    files.update(
        _tree_files(
            payload["qemu"] / flavor / "guest", f"qemu/{flavor}/guest"
        )
    )
    return _prepare_bundle(
        output,
        files=files,
        metadata={
            "kind": "flavor-qualification",
            "flavor": flavor,
            "producer_manifests_omitted": True,
        },
    )


def prepare_pull_request_repository_evidence(
    output: Path,
    *,
    unsigned_result: Path,
    signature_result: Path,
    repository_result: Path,
    qualification_outcome: str,
) -> Path:
    """Export one exact bounded artifact from the disposable APT qualification."""

    if qualification_outcome not in ("success", "failure", "cancelled"):
        raise ValueError("pull-request qualification outcome is invalid")
    candidates = (
        ("unsigned/evidence", unsigned_result / "evidence"),
        ("signature/evidence", signature_result / "evidence"),
        ("repository/evidence", repository_result / "evidence"),
        ("repository/client", repository_result / "client"),
    )
    files: dict[str, Path] = {}
    present: list[str] = []
    omitted = frozenset(("evidence.sha256",))
    for destination, source in candidates:
        if not source.exists() and not source.is_symlink():
            continue
        files.update(
            _tree_files(
                source,
                destination,
                omit_names=omitted,
                omit_suffixes=(".deb",),
            )
        )
        present.append(destination)

    complete = len(present) == len(candidates)
    if qualification_outcome == "success":
        if not complete:
            raise ValueError("successful APT qualification lacks bounded evidence")
        for label, result in (
            ("unsigned", unsigned_result),
            ("signature", signature_result),
            ("repository", repository_result),
        ):
            status = _read_environment(result / "evidence/result.env", label)
            if status.get("status") != "PASS" or status.get("publishable") != "false":
                raise ValueError(f"successful {label} evidence is contradictory")
        client = _read_environment(repository_result / "client/result.env", "client")
        if client.get("status") != "PASS":
            raise ValueError("successful APT client evidence is contradictory")

    return _prepare_bundle(
        output,
        files=files,
        metadata={
            "kind": "pull-request-repository-qualification",
            "qualification_outcome": qualification_outcome,
            "complete": complete,
            "source_groups": present,
            "producer_manifests_omitted": True,
        },
    )
