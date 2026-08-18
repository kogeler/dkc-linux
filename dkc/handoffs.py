"""Strict readers for data passed between release jobs.

Checksums make transport corruption visible, but they do not authenticate a
downloaded directory: an altered payload can be accompanied by altered
checksums.  These readers therefore combine an exact file boundary with typed
records, derived-field checks, and (for repository state) OpenPGP signatures.
Every consumer uses the same reader instead of selecting a few fields itself.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import subprocess
import tempfile
from typing import Any

from .evidence import verify_evidence_directory
from .schema import validate
from .source_discovery import make_variables
from .state import AuthoritativeState, parse_manifest, parse_state_pointer

__all__ = [
    "load_authoritative_state_handoff",
    "load_live_pool_handoff",
    "load_source_handoff",
]


_FINGERPRINT_RE = re.compile(r"^[0-9A-F]{40}$")


def _json_object(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_source_handoff(root: pathlib.Path) -> dict[str, object]:
    """Load the exact successful source-discovery handoff."""

    paths = verify_evidence_directory(root)
    if paths != ("result.env", "source-inventory.json", "source.env"):
        raise ValueError("source discovery has an unexpected file boundary")
    inventory = _json_object(root / "source-inventory.json", "source inventory")
    validate("source-inventory", inventory)
    values = make_variables(inventory)
    expected_environment = "".join(
        f"{name}={value}\n" for name, value in sorted(values.items())
    )
    if (root / "source.env").read_text(encoding="utf-8") != expected_environment:
        raise ValueError("source environment differs from its typed inventory")
    if (root / "result.env").read_text(encoding="utf-8") != (
        "status=PASS\nsource_discovery=PASS\n"
    ):
        raise ValueError("source discovery handoff is not successful")
    return inventory


def _fingerprints(path: pathlib.Path) -> frozenset[str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("tracked signing-subkey inventory is not a plain file")
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("tracked signing-subkey inventory is not ASCII text") from exc
    if (
        not lines
        or any(not _FINGERPRINT_RE.fullmatch(value) for value in lines)
        or len(lines) != len(set(lines))
    ):
        raise ValueError("tracked signing-subkey inventory is invalid")
    return frozenset(lines)


def _verify_signature(
    signature: pathlib.Path,
    *,
    keyring: pathlib.Path,
    fingerprints: frozenset[str],
    signed: pathlib.Path | None = None,
    output: pathlib.Path | None = None,
) -> None:
    for path, label in ((signature, "signature"), (keyring, "public keyring")):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"authoritative state {label} is not a plain file")
    if signed is not None and (not signed.is_file() or signed.is_symlink()):
        raise ValueError("authoritative state signed payload is not a plain file")
    command = ["gpgv", "--status-fd=1", "--keyring", str(keyring)]
    if output is not None:
        command.extend(("--output", str(output)))
    command.append(str(signature))
    if signed is not None:
        command.append(str(signed))
    result = subprocess.run(command, capture_output=True, check=False)
    valid = [
        fields[2]
        for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ")
        and len(fields := line.split()) > 2
    ]
    if result.returncode or len(valid) != 1 or valid[0] not in fingerprints:
        raise ValueError("authoritative state signature verification failed")


def load_authoritative_state_handoff(
    root: pathlib.Path,
    *,
    keyring: pathlib.Path,
    signing_subkeys: pathlib.Path,
) -> AuthoritativeState | None:
    """Authenticate an exact EMPTY or PRESENT repository-state handoff."""

    paths = verify_evidence_directory(root)
    status = _json_object(root / "state-status.json", "authoritative state status")
    validate("authoritative-state-read", status)
    state_status = status["status"]
    expected_result = (
        f"status=PASS\nauthoritative_state={state_status}\n"
    )
    if (root / "result.env").read_text(encoding="utf-8") != expected_result:
        raise ValueError("authoritative state result differs from its typed status")
    if state_status == "EMPTY":
        if paths != ("result.env", "state-status.json"):
            raise ValueError("empty authoritative state has an unexpected file boundary")
        return None

    expected_paths = (
        "result.env",
        "state-status.json",
        "state/current.asc",
        "state/manifest.json",
        "state/manifest.json.asc",
        "state/pointer.json",
    )
    if paths != expected_paths:
        raise ValueError("present authoritative state has an unexpected file boundary")
    fingerprints = _fingerprints(signing_subkeys)
    with tempfile.TemporaryDirectory(prefix="state-handoff-") as directory:
        extracted_pointer = pathlib.Path(directory) / "pointer.json"
        _verify_signature(
            root / "state/current.asc",
            keyring=keyring,
            fingerprints=fingerprints,
            output=extracted_pointer,
        )
        if extracted_pointer.read_bytes() != (root / "state/pointer.json").read_bytes():
            raise ValueError("state pointer differs from its signed payload")
    _verify_signature(
        root / "state/manifest.json.asc",
        keyring=keyring,
        fingerprints=fingerprints,
        signed=root / "state/manifest.json",
    )
    pointer = parse_state_pointer((root / "state/pointer.json").read_bytes())
    manifest_path = root / "state/manifest.json"
    if _sha256(manifest_path) != pointer.manifest_sha256:
        raise ValueError("authoritative manifest hash differs from the state pointer")
    manifest = parse_manifest(manifest_path.read_bytes())
    return AuthoritativeState(
        pointer,
        manifest,
        str(status["state_etag"]),
        str(status["manifest_etag"]),
        int(status["storage_object_count"]),
        int(status["storage_size"]),
    )


def load_live_pool_handoff(
    root: pathlib.Path,
    state: AuthoritativeState | None,
) -> pathlib.Path:
    """Load an exact pool export and bind every byte to signed live state."""

    paths = verify_evidence_directory(root)
    summary = _json_object(root / "summary.json", "live-pool export summary")
    validate("pool-export", summary)
    pool = root / "pool"
    if not pool.is_dir() or pool.is_symlink():
        raise ValueError("live-pool handoff lacks a plain pool directory")
    if state is None:
        if (
            summary["state"] != "EMPTY"
            or summary["object_count"] != 0
            or summary["size"] != 0
            or paths != ("summary.json",)
            or any(pool.iterdir())
        ):
            raise ValueError("empty live-pool handoff has an unexpected boundary")
        return pool
    if summary["state"] != "PRESENT":
        raise ValueError("live-pool handoff does not represent present state")
    expected = {
        artifact.key: artifact
        for artifact in state.manifest.artifacts
        if artifact.key.startswith("pool/")
        and artifact.key in state.manifest.live_objects
    }
    if not expected:
        raise ValueError("present authoritative state has no live pool")
    actual: dict[str, pathlib.Path] = {}
    for path in pool.rglob("*"):
        if path.is_dir():
            continue
        key = (pathlib.PurePosixPath("pool") / path.relative_to(pool)).as_posix()
        actual[key] = path
    expected_paths = tuple(sorted(["summary.json", *(key for key in actual)]))
    if paths != expected_paths or set(actual) != set(expected):
        raise ValueError("live-pool handoff differs from its signed inventory")
    total_size = 0
    for key, path in actual.items():
        artifact = expected[key]
        size = path.stat().st_size
        if (
            artifact.cache_class != "immutable"
            or artifact.size != size
            or artifact.sha256 != _sha256(path)
        ):
            raise ValueError("live-pool object differs from its signed identity")
        total_size += size
    if summary["object_count"] != len(actual) or summary["size"] != total_size:
        raise ValueError("live-pool summary differs from its exact payload")
    return pool
