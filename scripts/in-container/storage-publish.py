#!/usr/bin/env python3
"""Publish one verified signed repository through the conditional S3 protocol."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone

from dkc.gc import execute_gc, plan_gc
from dkc.lease import LeaseHandle, LeaseManager
from dkc.publication import PublicationExecutor
from dkc.publication_plan import plan_repository
from dkc.records import LeaseOwner
from dkc.remote_store import S3ObjectStore
from dkc.s3 import S3Client
from dkc.state import parse_manifest, parse_state_pointer
from dkc.storage_budget import project_storage
from dkc.storage_connection import load_connection
from dkc.storage_output import StorageRedactor, run_with_sanitized_output
from dkc.storage_repository import load_verified_repository


def tracked_fingerprints(path: pathlib.Path) -> set[str]:
    values = set(path.read_text(encoding="ascii").splitlines())
    if not values or any(len(value) != 40 for value in values):
        raise ValueError("tracked signing-subkey inventory is invalid")
    return values


def verify(
    signature: pathlib.Path,
    *,
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
        raise ValueError("repository signature verification failed")


def desired_pointer(
    repository: pathlib.Path,
    *,
    keyring: pathlib.Path,
    fingerprints: set[str],
    workspace: pathlib.Path,
):
    payload = workspace / "desired-pointer.json"
    verify(
        repository / "state/current.asc",
        keyring=keyring,
        fingerprints=fingerprints,
        output=payload,
    )
    pointer = parse_state_pointer(payload.read_bytes())
    manifest = repository / pointer.manifest_key
    signature = repository / f"{pointer.manifest_key}.asc"
    if not manifest.is_file() or not signature.is_file():
        raise ValueError("desired state references an incomplete manifest")
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != pointer.manifest_sha256:
        raise ValueError("desired state manifest hash is inconsistent")
    verify(
        signature,
        keyring=keyring,
        fingerprints=fingerprints,
        signed=manifest,
    )
    verify(
        repository / "dists/trixie/InRelease",
        keyring=keyring,
        fingerprints=fingerprints,
    )
    verify(
        repository / "manifest.json.asc",
        keyring=keyring,
        fingerprints=fingerprints,
        signed=repository / "manifest.json",
    )
    return pointer, parse_manifest(manifest.read_bytes())


def verify_current_generation(
    store: S3ObjectStore,
    desired_body: bytes,
    desired,
    desired_manifest,
    *,
    keyring: pathlib.Path,
    fingerprints: set[str],
    workspace: pathlib.Path,
) -> None:
    current = store.get("state/current.asc")
    if current is None:
        if desired.generation != 0 or desired.previous_generation is not None:
            raise ValueError("non-bootstrap publication found no authoritative state")
        return
    if current.body == desired_body:
        if current.metadata.cache_control != "public, max-age=0, must-revalidate":
            raise ValueError("committed state has unexpected HTTP metadata")
        return
    current_signature = workspace / "current.asc"
    current_payload = workspace / "current.json"
    current_signature.write_bytes(current.body)
    verify(
        current_signature,
        keyring=keyring,
        fingerprints=fingerprints,
        output=current_payload,
    )
    observed = parse_state_pointer(current_payload.read_bytes())
    if (
        desired.generation != observed.generation + 1
        or desired.previous_generation != observed.generation
        or desired_manifest.previous_publication
        != {
            "publication_id": observed.publication_id,
            "generation": observed.generation,
        }
    ):
        raise ValueError("desired publication does not follow authoritative state")


def terminal_proof(token: str, old: LeaseOwner) -> bool:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{old.repository}/actions/runs/{old.workflow_run_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}" if token else "",
            "User-Agent": "dkc-storage-lifecycle",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(1_048_577)
    except (OSError, urllib.error.URLError):
        return False
    if len(body) > 1_048_576:
        return False
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    repository = value.get("head_repository") if isinstance(value, dict) else None
    return bool(
        isinstance(repository, dict)
        and repository.get("full_name") == old.repository
        and value.get("status") == "completed"
        and value.get("run_attempt") == int(old.run_attempt)
    )


def main(args: argparse.Namespace) -> int:
    output = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    connection = load_connection(args.connection)
    token = args.github_token_file.read_text(encoding="utf-8").strip()
    client = S3Client(connection.endpoint, connection.credentials)
    store = S3ObjectStore(client)
    inventory = load_verified_repository(args.repository_result)
    fingerprints = tracked_fingerprints(args.signing_subkeys)
    lease: LeaseHandle | None = None
    published = False
    with tempfile.TemporaryDirectory(prefix="publication-") as directory:
        workspace = pathlib.Path(directory)
        desired, desired_manifest = desired_pointer(
            args.repository_result / "repository",
            keyring=args.keyring,
            fingerprints=fingerprints,
            workspace=workspace,
        )
        owner = LeaseOwner(
            repository=args.canonical_repository,
            workflow_run_id=args.workflow_run_id,
            run_attempt=args.run_attempt,
            operation="publish",
            nonce=secrets.token_hex(16),
        )
        manager = LeaseManager(
            store,
            ttl_seconds=args.lease_ttl_seconds,
            takeover_grace_seconds=args.takeover_grace_seconds,
            terminal_proof=lambda old: terminal_proof(token, old),
        )
        try:
            lease = manager.acquire(owner)
            verify_current_generation(
                store,
                (args.repository_result / "repository/state/current.asc").read_bytes(),
                desired,
                desired_manifest,
                keyring=args.keyring,
                fingerprints=fingerprints,
                workspace=workspace,
            )
            plan = plan_repository(
                inventory, store, max_object_bytes=args.max_object_bytes
            )
            gc_plan = plan_gc(
                desired_manifest,
                store,
                now=datetime.now(timezone.utc),
                max_objects=args.gc_max_objects,
                max_bytes=args.gc_max_bytes,
            )
            projection = project_storage(
                store.list_objects(""),
                inventory,
                (target.key for target in gc_plan.targets),
            )
            if (
                desired_manifest.retention_mode == "series-size"
                and desired_manifest.retention_max_bytes is not None
                and projection.size > desired_manifest.retention_max_bytes
            ):
                raise ValueError(
                    "projected object storage exceeds the signed retention limit"
                )

            def checkpoint() -> None:
                nonlocal lease
                assert lease is not None
                manager.assert_batch_window(
                    lease, batch_timeout_seconds=120, safety_seconds=60
                )
                lease = manager.renew(lease)

            PublicationExecutor(store).execute(
                plan, mutation_checkpoint=checkpoint
            )
            desired_state_body = (
                args.repository_result / "repository/state/current.asc"
            ).read_bytes()

            def observed_generation() -> int:
                current = store.get("state/current.asc")
                return (
                    desired.generation
                    if current is not None and current.body == desired_state_body
                    else -1
                )

            deleted = execute_gc(
                gc_plan,
                store,
                observed_generation=observed_generation,
                mutation_checkpoint=checkpoint,
            )
        except BaseException:
            if lease is not None:
                try:
                    lease = manager.release(lease)
                except BaseException:
                    print(
                        "warning: publication failed and the lease could not be "
                        "released; safe takeover requires expiry and terminal-run proof",
                        file=sys.stderr,
                    )
            raise
        else:
            if lease is not None:
                lease = manager.release(lease)
        final_inventory = store.list_objects("")
        final_storage_size = sum(item.size for item in final_inventory)
        if (
            desired_manifest.retention_mode == "series-size"
            and desired_manifest.retention_max_bytes is not None
            and final_storage_size > desired_manifest.retention_max_bytes
        ):
            raise RuntimeError(
                "committed object storage exceeds the signed retention limit"
            )
        published = True

    summary = {
        "generation": desired.generation,
        "object_count": len(inventory),
        "storage_object_count": len(final_inventory),
        "storage_size": final_storage_size,
        "gc_deleted_bytes": sum(target.size for target in gc_plan.targets),
        "gc_deleted_objects": len(deleted),
        "publication_id_sha256": hashlib.sha256(
            desired.publication_id.encode("utf-8")
        ).hexdigest(),
        "schema": "dkc.storage-publication-result.v1",
        "status": "PASS" if published else "FAIL",
    }
    (output / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    (output / "result.env").write_text(
        "status=PASS\nstorage_publication=PASS\n",
        encoding="utf-8",
    )
    records = []
    for path in sorted(output.iterdir()):
        records.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (output / "evidence.sha256").write_text(
        "\n".join(records) + "\n", encoding="utf-8"
    )
    print("PASS signed repository committed and verified through authenticated storage")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-result", type=pathlib.Path, required=True)
    parser.add_argument("--connection", type=pathlib.Path, required=True)
    parser.add_argument("--keyring", type=pathlib.Path, required=True)
    parser.add_argument("--signing-subkeys", type=pathlib.Path, required=True)
    parser.add_argument("--github-token-file", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--canonical-repository", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--max-object-bytes", type=int, required=True)
    parser.add_argument("--lease-ttl-seconds", type=int, default=900)
    parser.add_argument("--takeover-grace-seconds", type=int, default=300)
    parser.add_argument("--gc-max-objects", type=int, required=True)
    parser.add_argument("--gc-max-bytes", type=int, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    parsed = parse_args()
    try:
        github_token = parsed.github_token_file.read_text(encoding="utf-8").strip()
        output_redactor = StorageRedactor.from_path(
            parsed.connection, additional_values=(github_token,)
        )
    except BaseException:
        print("FAIL unable to initialize storage output sanitizer", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(
        run_with_sanitized_output(output_redactor, lambda: main(parsed))
    )
