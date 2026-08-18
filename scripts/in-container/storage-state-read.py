#!/usr/bin/env python3
"""Read and verify the authoritative signed state through authenticated S3."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import subprocess
import sys

from dkc.handoffs import load_authoritative_state_handoff
from dkc.s3 import S3Client
from dkc.state import AuthoritativeState, parse_manifest, parse_state_pointer
from dkc.storage_connection import load_connection
from dkc.storage_output import StorageRedactor, run_with_sanitized_output
from dkc.storage import ObjectMetadata
from dkc.storage_repository import IMMUTABLE_CACHE, MUTABLE_CACHE


def atomic_json(path: pathlib.Path, value: object) -> None:
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    pending.replace(path)


def allowed_fingerprints(path: pathlib.Path) -> set[str]:
    values = set(path.read_text(encoding="ascii").splitlines())
    if not values or any(len(value) != 40 for value in values):
        raise ValueError("tracked signing-subkey inventory is invalid")
    return values


def verify_signature(
    *,
    signature: pathlib.Path,
    keyring: pathlib.Path,
    fingerprints: set[str],
    signed: pathlib.Path | None = None,
    output: pathlib.Path | None = None,
) -> None:
    command = ["gpgv", "--status-fd=1", "--keyring", str(keyring)]
    if output is not None:
        command.extend(("--output", str(output)))
    command.append(str(signature))
    if signed is not None:
        command.append(str(signed))
    result = subprocess.run(command, capture_output=True, check=False)
    valid = [
        line.split()[2]
        for line in result.stdout.decode("utf-8", errors="replace").splitlines()
        if line.startswith("[GNUPG:] VALIDSIG ") and len(line.split()) > 2
    ]
    if result.returncode or len(valid) != 1 or valid[0] not in fingerprints:
        raise ValueError("authoritative state signature verification failed")


def evidence(output: pathlib.Path) -> None:
    checksum = output / "evidence.sha256"
    records = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path != checksum:
            records.append(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                f"{path.relative_to(output).as_posix()}"
            )
    checksum.write_text("\n".join(records) + "\n", encoding="utf-8")


def main(args: argparse.Namespace) -> int:
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    connection = load_connection(args.connection)
    client = S3Client(connection.endpoint, connection.credentials)
    storage_inventory = client.list_objects("")
    storage_object_count = len(storage_inventory)
    storage_size = sum(item.size for item in storage_inventory)
    current = client.get_optional("state/current.asc")
    if current is None:
        atomic_json(
            args.output / "state-status.json",
            {
                "authoritative": True,
                "schema": "dkc.authoritative-state-read.v1",
                "storage_object_count": storage_object_count,
                "storage_size": storage_size,
                "status": "EMPTY",
            },
        )
        (args.output / "result.env").write_text(
            "status=PASS\nauthoritative_state=EMPTY\n", encoding="utf-8"
        )
        evidence(args.output)
        load_authoritative_state_handoff(
            args.output,
            keyring=args.keyring,
            signing_subkeys=args.signing_subkeys,
        )
        print("PASS authoritative state read found an empty repository")
        return 0
    if current.metadata != ObjectMetadata("application/pgp-signature", MUTABLE_CACHE):
        raise ValueError("authoritative state pointer has unexpected HTTP metadata")

    state_dir = args.output / "state"
    state_dir.mkdir()
    current_path = state_dir / "current.asc"
    current_path.write_bytes(current.body)
    pointer_path = state_dir / "pointer.json"
    fingerprints = allowed_fingerprints(args.signing_subkeys)
    verify_signature(
        signature=current_path,
        keyring=args.keyring,
        fingerprints=fingerprints,
        output=pointer_path,
    )
    pointer = parse_state_pointer(pointer_path.read_bytes())
    manifest_object = client.get_optional(pointer.manifest_key)
    signature_object = client.get_optional(f"{pointer.manifest_key}.asc")
    if manifest_object is None or signature_object is None:
        raise ValueError("authoritative state references an incomplete publication")
    if manifest_object.metadata != ObjectMetadata("application/json", IMMUTABLE_CACHE):
        raise ValueError("authoritative manifest has unexpected HTTP metadata")
    if signature_object.metadata != ObjectMetadata(
        "application/pgp-signature", IMMUTABLE_CACHE
    ):
        raise ValueError("authoritative manifest signature has unexpected HTTP metadata")
    if hashlib.sha256(manifest_object.body).hexdigest() != pointer.manifest_sha256:
        raise ValueError("authoritative manifest hash differs from the state pointer")
    manifest_path = state_dir / "manifest.json"
    signature_path = state_dir / "manifest.json.asc"
    manifest_path.write_bytes(manifest_object.body)
    signature_path.write_bytes(signature_object.body)
    verify_signature(
        signature=signature_path,
        signed=manifest_path,
        keyring=args.keyring,
        fingerprints=fingerprints,
    )
    manifest = parse_manifest(manifest_object.body)
    AuthoritativeState(
        pointer,
        manifest,
        current.etag,
        manifest_object.etag,
        storage_object_count,
        storage_size,
    )
    atomic_json(
        args.output / "state-status.json",
        {
            "authoritative": True,
            "manifest_etag": manifest_object.etag,
            "schema": "dkc.authoritative-state-read.v1",
            "state_etag": current.etag,
            "storage_object_count": storage_object_count,
            "storage_size": storage_size,
            "status": "PRESENT",
        },
    )
    (args.output / "result.env").write_text(
        "status=PASS\nauthoritative_state=PRESENT\n", encoding="utf-8"
    )
    evidence(args.output)
    load_authoritative_state_handoff(
        args.output,
        keyring=args.keyring,
        signing_subkeys=args.signing_subkeys,
    )
    print("PASS authoritative signed state verified")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection", type=pathlib.Path, required=True)
    parser.add_argument("--keyring", type=pathlib.Path, required=True)
    parser.add_argument("--signing-subkeys", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    parsed = parse_args()
    try:
        redactor = StorageRedactor.from_path(parsed.connection)
    except BaseException:
        print("FAIL unable to initialize storage output sanitizer", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(run_with_sanitized_output(redactor, lambda: main(parsed)))
