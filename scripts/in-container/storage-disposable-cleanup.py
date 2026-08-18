#!/usr/bin/env python3
"""Recover cleanup of one recorded disposable S3 prefix."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from dkc.s3 import S3Client
from dkc.storage_connection import MissingStorageConnection, load_connection
from dkc.storage_disposable import cleanup_disposable_prefix
from dkc.storage_output import StorageRedactor, run_with_sanitized_output
from dkc.storage_repository import validate_disposable_prefix


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True, type=Path)
    parser.add_argument("--connection", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def load_prefix(result: Path) -> str:
    root = result.resolve(strict=True)
    journal = root / "cleanup.json"
    if not journal.is_file() or journal.stat().st_size > 4096:
        raise ValueError("disposable result has no bounded cleanup journal")
    try:
        value = json.loads(journal.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("disposable cleanup journal is invalid JSON") from exc
    if not isinstance(value, dict) or value.get("schema") != (
        "dkc.storage-disposable-cleanup-journal.v1"
    ):
        raise ValueError("disposable cleanup journal has the wrong schema")
    prefix = value.get("prefix")
    if not isinstance(prefix, str):
        raise ValueError("disposable cleanup journal has no prefix")
    return validate_disposable_prefix(prefix)


def key_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def finalize(output: Path, status: str, error: str = "") -> None:
    lines = [f"status={status}", f"cleanup={status}"]
    if error:
        lines.append(f"error_sha256={hashlib.sha256(error.encode()).hexdigest()}")
    (output / "result.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    checksum = output / "evidence.sha256"
    entries = []
    for path in sorted(output.rglob("*")):
        if path == checksum or not path.is_file():
            continue
        key = path.relative_to(output).as_posix()
        entries.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {key}")
    checksum.write_text("\n".join(entries) + "\n", encoding="utf-8")


def main(args: argparse.Namespace, redactor: StorageRedactor) -> int:
    output: Path = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    connection = None
    try:
        connection = load_connection(args.connection)
        prefix = load_prefix(args.result)
        events = cleanup_disposable_prefix(
            S3Client(connection.endpoint, connection.credentials), prefix
        )
        atomic_json(
            output / "events.json",
            {
                "events": [
                    {
                        "detail": event.detail,
                        "key_sha256": key_digest(event.key),
                        "operation": event.operation,
                        "status": event.status,
                    }
                    for event in events
                ],
                "prefix_sha256": key_digest(prefix),
                "schema": "dkc.storage-disposable-cleanup.v1",
                "status": "PASS",
            },
        )
        finalize(output, "PASS")
        return 0
    except BaseException as exc:
        message = str(exc)
        message = redactor.redact(message).replace("\n", " ")[:2048]
        status = "BLOCKED" if isinstance(exc, MissingStorageConnection) else "FAIL"
        atomic_json(
            output / "summary.json",
            {
                "error": message,
                "schema": "dkc.storage-disposable-cleanup.v1",
                "status": status,
            },
        )
        finalize(output, status, message)
        print(f"{status} disposable storage cleanup: {message}", file=sys.stderr)
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
