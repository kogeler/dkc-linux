"""Replay a verified bootstrap repository against a local HTTPS S3 fixture."""

from __future__ import annotations

import argparse
import functools
import hashlib
import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace

from dkc.cli import storage_publish as publisher
from dkc.storage_output import StorageRedactor, run_with_sanitized_output
from dkc.storage_progress import StorageProgress
from tests.http_storage import HttpStorage, certificate


def main(repository: pathlib.Path, output: pathlib.Path) -> None:
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="storage-replay-") as directory:
        workspace = pathlib.Path(directory)
        cert, key = certificate(workspace)
        os.environ["SSL_CERT_FILE"] = str(cert)
        fixture = HttpStorage(cert, key)
        try:
            connection = workspace / "connection.json"
            connection.write_text(
                json.dumps(
                    {
                        "s3_access_key_id": "fixture-access",
                        "s3_secret_access_key": "fixture-secret",
                        "s3_endpoint": fixture.client.endpoint.base_url,
                        "s3_bucket": "fixture",
                        "s3_region": "test-region",
                        "s3_addressing_style": "path",
                    }
                )
            )
            connection.chmod(0o600)
            token = workspace / "token"
            token.write_text("fixture-token")
            keys = repository / "repository/keys"
            args = SimpleNamespace(
                output=output / "initial",
                connection=connection,
                github_token_file=token,
                repository_result=repository,
                signing_subkeys=keys / "archive-signing-subkeys.fingerprints",
                keyring=keys / "dkc-archive-keyring.gpg",
                canonical_repository="fixture/repository",
                workflow_run_id="1",
                run_attempt="1",
                lease_ttl_seconds=900,
                takeover_grace_seconds=300,
                max_object_bytes=2_000_000_000,
                gc_max_objects=10_000,
                gc_max_bytes=10_000_000_000,
            )
            pointer, _ = publisher.desired_pointer(
                repository / "repository",
                keyring=args.keyring,
                fingerprints=publisher.tracked_fingerprints(args.signing_subkeys),
                workspace=workspace,
            )
            if pointer.generation != 0:
                raise ValueError(
                    "local replay requires a verified generation-zero repository"
                )
            redactor = StorageRedactor.from_path(
                connection, additional_values=("fixture-token",)
            )
            for name in ("initial", "retry"):
                args.output = output / name
                fixture.reset_counters()
                progress = StorageProgress(redactor, sys.stdout)
                status = run_with_sanitized_output(
                    redactor, functools.partial(publisher.main, args, progress)
                )
                if status:
                    raise RuntimeError("local HTTPS publication replay failed")
            pool_keys = fixture.store.list_keys("pool/")
            pool_bytes = sum(len(fixture.store.get(key).body) for key in pool_keys)
            uploaded = sum(fixture.received_bytes[key] for key in pool_keys)
            downloaded = sum(fixture.returned_bytes[key] for key in pool_keys)
            if (
                uploaded
                or downloaded != pool_bytes
                or any(
                    fixture.requests["GET", key] != 1
                    or fixture.requests["HEAD", key] != 1
                    or fixture.requests["PUT", key] != 0
                    for key in pool_keys
                )
            ):
                raise RuntimeError(
                    "retry transferred an immutable pool payload redundantly"
                )
            (output / "summary.json").write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "pool_objects": len(pool_keys),
                        "pool_bytes": pool_bytes,
                        "retry_uploaded_pool_bytes": uploaded,
                        "retry_downloaded_pool_bytes": downloaded,
                        "publishable": False,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        finally:
            fixture.close()
    records = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
        f"{path.relative_to(output).as_posix()}"
        for path in sorted(output.rglob("*"))
        if path.is_file()
    ]
    (output / "evidence.sha256").write_text("\n".join(records) + "\n")
    print("PASS complete repository publication and reuse through local HTTPS storage")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository-result", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parsed = parser.parse_args()
    main(parsed.repository_result, parsed.output)
