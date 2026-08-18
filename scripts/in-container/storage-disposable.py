#!/usr/bin/env python3
"""Qualify one complete repository against an exact disposable storage prefix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

from dkc.s3 import S3Client
from dkc.storage_connection import MissingStorageConnection, load_connection
from dkc.storage_disposable import (
    DisposableConfig,
    DisposableIntegration,
    IntegrationEvent,
)
from dkc.storage_output import StorageRedactor, run_with_sanitized_output
from dkc.storage_repository import load_verified_repository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository-result", required=True, type=Path)
    parser.add_argument("--connection", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--canonical-repository", required=True)
    return parser.parse_args()


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def key_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def event_evidence(event: IntegrationEvent) -> dict[str, str]:
    return {
        "detail": event.detail,
        "key_sha256": key_digest(event.key),
        "operation": event.operation,
        "status": event.status,
    }


def finalize_evidence(output: Path, status: str, error: str = "") -> None:
    lines = [
        f"status={status}",
        f"storage_disposable={'PASS' if status == 'PASS' else status}",
        f"publishable=false",
    ]
    if error:
        lines.append(f"error_sha256={hashlib.sha256(error.encode()).hexdigest()}")
    (output / "result.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    checksum_path = output / "evidence.sha256"
    entries: list[str] = []
    for path in sorted(output.rglob("*")):
        if path == checksum_path or not path.is_file():
            continue
        key = path.relative_to(output).as_posix()
        if key == "cleanup.json":
            continue
        entries.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {key}")
    checksum_path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def main(args: argparse.Namespace, redactor: StorageRedactor) -> int:
    output: Path = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    connection = None
    integration: DisposableIntegration | None = None
    try:
        connection = load_connection(args.connection)
        nonce = secrets.token_hex(16)
        disposable = DisposableConfig(
            canonical_repository=args.canonical_repository,
            run_id=args.run_id,
            nonce=nonce,
        )
        inventory = load_verified_repository(args.repository_result)
        cleanup_path = output / "cleanup.json"
        atomic_json(
            cleanup_path,
            {
                "prefix": disposable.prefix,
                "schema": "dkc.storage-disposable-cleanup-journal.v1",
            },
        )
        cleanup_path.chmod(0o600)
        atomic_json(
            output / "inventory.json",
            {
                "objects": [
                    {
                        "cache_control": item.metadata.cache_control,
                        "content_type": item.metadata.content_type,
                        "key_sha256": key_digest(item.relative_key),
                        "sha256": item.sha256,
                        "size": item.size,
                    }
                    for item in inventory
                ],
                "prefix_sha256": key_digest(disposable.prefix),
                "schema": "dkc.storage-disposable-inventory.v1",
            },
        )
        integration = DisposableIntegration(
            S3Client(connection.endpoint, connection.credentials), disposable
        )
        events = integration.run(inventory)
        atomic_json(
            output / "events.json",
            {
                "events": [event_evidence(event) for event in events],
                "schema": "dkc.storage-disposable-events.v1",
            },
        )
        atomic_json(
            output / "summary.json",
            {
                "authoritative_reads": sum(
                    event.operation == "authoritative-read" for event in events
                ),
                "cas": "PASS",
                "cleanup": "PASS",
                "prefix_sha256": key_digest(disposable.prefix),
                "repository_objects": len(inventory),
                "schema": "dkc.storage-disposable-summary.v1",
                "status": "PASS",
            },
        )
        finalize_evidence(output, "PASS")
        return 0
    except BaseException as exc:
        message = str(exc)
        message = redactor.redact(message).replace("\n", " ")[:2048]
        if integration is not None:
            atomic_json(
                output / "events.json",
                {
                    "events": [event_evidence(event) for event in integration.events],
                    "schema": "dkc.storage-disposable-events.v1",
                },
            )
        status = "BLOCKED" if isinstance(exc, MissingStorageConnection) else "FAIL"
        atomic_json(
            output / "summary.json",
            {
                "error": message,
                "schema": "dkc.storage-disposable-summary.v1",
                "status": status,
            },
        )
        finalize_evidence(output, status, message)
        print(f"{status} disposable storage integration: {message}", file=sys.stderr)
        return 2 if status == "BLOCKED" else 1


if __name__ == "__main__":
    parsed_args = parse_args()
    try:
        output_redactor = StorageRedactor.from_path(parsed_args.connection)
    except BaseException:
        print("FAIL unable to initialize storage output sanitizer", file=sys.stderr)
        raise SystemExit(1) from None
    raise SystemExit(
        run_with_sanitized_output(
            output_redactor, lambda: main(parsed_args, output_redactor)
        )
    )
