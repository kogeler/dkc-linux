"""Publish one audited documentation-site build below the site/ storage prefix."""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import secrets
import sys

from dkc.github_runs import workflow_attempt_terminal
from dkc.lease import LeaseHandle, LeaseManager
from dkc.records import LeaseOwner
from dkc.remote_store import S3ObjectStore
from dkc.s3 import S3Client
from dkc.serialize import canonical_bytes
from dkc.site_publication import (
    SITE_PREFIX,
    acquire_lease,
    execute_site_plan,
    load_site_build,
    plan_site_publication,
    verify_site_publication,
)
from dkc.storage_connection import load_connection
from dkc.storage_output import StorageRedactor, run_with_sanitized_output


def main(args: argparse.Namespace) -> int:
    output = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    build = load_site_build(args.site_result)
    connection = load_connection(args.connection)
    token = args.github_token_file.read_text(encoding="utf-8").strip()
    store = S3ObjectStore(S3Client(connection.endpoint, connection.credentials))
    owner = LeaseOwner(
        repository=args.canonical_repository,
        workflow_run_id=args.workflow_run_id,
        run_attempt=args.run_attempt,
        operation="site-publish",
        nonce=secrets.token_hex(16),
    )
    manager = LeaseManager(
        store,
        ttl_seconds=args.lease_ttl_seconds,
        takeover_grace_seconds=args.takeover_grace_seconds,
        terminal_proof=lambda old: workflow_attempt_terminal(token, old),
    )
    lease: LeaseHandle | None = acquire_lease(
        manager,
        owner,
        wait_seconds=args.lease_wait_seconds,
        on_wait=lambda: print("waiting for the production lease", file=sys.stderr),
    )
    try:
        plan = plan_site_publication(build, store, max_deletions=args.max_deletions)

        def checkpoint() -> None:
            nonlocal lease
            assert lease is not None
            manager.assert_batch_window(
                lease, batch_timeout_seconds=120, safety_seconds=60
            )
            lease = manager.renew(lease)

        execute_site_plan(plan, store, mutation_checkpoint=checkpoint)
        verify_site_publication(build, store)
    except BaseException:
        if lease is not None:
            try:
                lease = manager.release(lease)
            except BaseException:
                print(
                    "warning: site publication failed and the lease could not be "
                    "released; safe takeover requires expiry and terminal-run proof",
                    file=sys.stderr,
                )
        raise
    assert lease is not None
    lease = manager.release(lease)

    # Pages whose bytes this publication changed or removed, as site keys. The
    # IndexNow notifier submits exactly these.
    changed = {write.key for write in plan.writes} | set(plan.deletions)
    summary = {
        "changed_pages": sorted(
            key.removeprefix(SITE_PREFIX) for key in changed if key.endswith(".html")
        ),
        "deleted_objects": len(plan.deletions),
        "object_count": len(build.manifest.objects),
        "schema": "dkc.site-publication-result.v1",
        "size": sum(item.size for item in build.manifest.objects),
        "source_revision": build.manifest.source_revision,
        "status": "PASS",
        "unchanged_objects": len(plan.unchanged),
        "written_objects": len(plan.writes),
    }
    (output / "summary.json").write_bytes(canonical_bytes(summary))
    records = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in sorted(output.iterdir())
    ]
    (output / "evidence.sha256").write_text("\n".join(records) + "\n", encoding="utf-8")
    print(
        "PASS site published and verified: "
        f"{len(plan.writes)} written, {len(plan.unchanged)} unchanged, "
        f"{len(plan.deletions)} deleted"
    )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-result", type=pathlib.Path, required=True)
    parser.add_argument("--connection", type=pathlib.Path, required=True)
    parser.add_argument("--github-token-file", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--canonical-repository", required=True)
    parser.add_argument("--workflow-run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--lease-ttl-seconds", type=int, required=True)
    parser.add_argument("--takeover-grace-seconds", type=int, required=True)
    parser.add_argument("--lease-wait-seconds", type=int, required=True)
    parser.add_argument("--max-deletions", type=int, required=True)
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
    raise SystemExit(run_with_sanitized_output(output_redactor, lambda: main(parsed)))
