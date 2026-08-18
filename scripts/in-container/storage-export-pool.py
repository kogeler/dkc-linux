#!/usr/bin/env python3
"""Export the authenticated live pool needed to build the next generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

from dkc.handoffs import (
    load_authoritative_state_handoff,
    load_live_pool_handoff,
)
from dkc.s3 import S3Client
from dkc.storage import ObjectMetadata
from dkc.storage_connection import load_connection
from dkc.storage_output import StorageRedactor, run_with_sanitized_output
from dkc.storage_repository import IMMUTABLE_CACHE


def main(args: argparse.Namespace) -> int:
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    state = load_authoritative_state_handoff(
        args.state_result,
        keyring=args.keyring,
        signing_subkeys=args.signing_subkeys,
    )
    pool = args.output / "pool"
    pool.mkdir()
    count = 0
    size = 0
    if state is not None:
        manifest = state.manifest
        artifacts = {item.key: item for item in manifest.artifacts}
        keys = sorted(key for key in manifest.live_objects if key.startswith("pool/"))
        if not keys:
            raise ValueError("present authoritative state has no live pool")
        connection = load_connection(args.connection)
        client = S3Client(connection.endpoint, connection.credentials)
        for key in keys:
            artifact = artifacts[key]
            if artifact.cache_class != "immutable":
                raise ValueError("live pool artifact is not immutable")
            remote = client.get_optional(key)
            expected_metadata = ObjectMetadata(artifact.media_type, IMMUTABLE_CACHE)
            if (
                remote is None
                or len(remote.body) != artifact.size
                or hashlib.sha256(remote.body).hexdigest() != artifact.sha256
                or remote.metadata != expected_metadata
            ):
                raise ValueError("authenticated live pool object failed verification")
            target = args.output / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(remote.body)
            count += 1
            size += len(remote.body)
    state_status = "PRESENT" if state is not None else "EMPTY"
    summary = {
        "object_count": count,
        "schema": "dkc.pool-export.v1",
        "size": size,
        "state": state_status,
        "status": "PASS",
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    checksum = args.output / "evidence.sha256"
    records = []
    for path in sorted(args.output.rglob("*")):
        if path.is_file() and path != checksum:
            records.append(
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
                f"{path.relative_to(args.output).as_posix()}"
            )
    checksum.write_text("\n".join(records) + "\n", encoding="utf-8")
    load_live_pool_handoff(args.output, state)
    print("PASS authenticated live pool export completed")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-result", type=pathlib.Path, required=True)
    parser.add_argument("--keyring", type=pathlib.Path, required=True)
    parser.add_argument("--signing-subkeys", type=pathlib.Path, required=True)
    parser.add_argument("--connection", type=pathlib.Path, required=True)
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
