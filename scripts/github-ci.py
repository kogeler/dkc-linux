#!/usr/bin/env python3
"""Small GitHub Actions adapters around tested project policy."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dkc.github import (
    authorize_lifecycle,
    export_image_bundle,
    export_lifecycle_outputs,
    export_source_environment,
    prepare_pull_request_qualification,
    require_terminal_result,
    write_run_identity,
    write_workflow_assignments,
)
from dkc.github_cache import delete_release_caches
from dkc.release_cache import (
    prepare_release_cache,
    verify_release_cache,
)
from dkc.records import LtoMode
from dkc.retention import RetentionMode
from dkc.serialize import boolean_text


def required_environment(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value:
        raise ValueError(f"required GitHub environment value is absent: {name}")
    return value


def lifecycle_gate() -> None:
    bootstrap = authorize_lifecycle(
        event=required_environment("GITHUB_EVENT_NAME"),
        repository=required_environment("GITHUB_REPOSITORY"),
        selected_ref=required_environment("GITHUB_REF"),
        canonical_repository=required_environment("GITHUB_CANONICAL_REPOSITORY"),
        confirm_lifecycle=os.environ.get("GITHUB_CONFIRM_LIFECYCLE", ""),
        allow_empty_bootstrap=os.environ.get("GITHUB_ALLOW_EMPTY_BOOTSTRAP", ""),
    )
    output = Path(required_environment("GITHUB_OUTPUT"))
    write_workflow_assignments(
        output, {"bootstrap_allowed": boolean_text(bootstrap)}
    )
    print("PASS production lifecycle trigger is authorized")


def run_identity(role: str) -> None:
    value = write_run_identity(
        environment_file=Path(required_environment("GITHUB_ENV")),
        repository=required_environment("GITHUB_REPOSITORY"),
        workflow_run_id=required_environment("GITHUB_RUN_ID"),
        run_attempt=required_environment("GITHUB_RUN_ATTEMPT"),
        role=role,
    )
    print(f"PASS workflow run identity prepared for {role}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("lifecycle-gate")
    identity = subparsers.add_parser("run-identity")
    identity.add_argument("--role", required=True)
    export = subparsers.add_parser("export-lifecycle")
    export.add_argument("--decision", type=Path, required=True)
    export.add_argument("--root", type=Path, required=True)
    qualification = subparsers.add_parser("qualification-decision")
    qualification.add_argument("--source", type=Path, required=True)
    qualification.add_argument("--decision", type=Path, required=True)
    qualification.add_argument("--root", type=Path, required=True)
    qualification.add_argument("--epoch", type=int, required=True)
    qualification.add_argument("--dkc-revision", type=int, required=True)
    qualification.add_argument(
        "--lto-mode", choices=("none", "thin", "full"), required=True
    )
    qualification.add_argument(
        "--retention-mode", choices=("series", "series-size"), required=True
    )
    qualification.add_argument("--retention-max-bytes")
    source = subparsers.add_parser("export-source")
    source.add_argument("--source", type=Path, required=True)
    images = subparsers.add_parser("export-image-bundle")
    images.add_argument("--input", type=Path, required=True)
    terminal = subparsers.add_parser("terminal-result")
    terminal.add_argument("--decision", required=True)
    terminal.add_argument("--decision-result", required=True)
    terminal.add_argument("--final-result", required=True)
    cache_prepare = subparsers.add_parser("release-cache-prepare")
    cache_prepare.add_argument("--cache", type=Path, required=True)
    cache_prepare.add_argument("--flavor-result", type=Path, required=True)
    cache_prepare.add_argument("--selftest-result", type=Path, required=True)
    cache_prepare.add_argument("--qemu-result", type=Path, required=True)
    cache_prepare.add_argument("--decision", type=Path, required=True)
    cache_prepare.add_argument("--flavor", required=True)
    cache_prepare.add_argument("--build-image", required=True)
    cache_prepare.add_argument("--toolbox-image", required=True)
    cache_prepare.add_argument("--key", required=True)
    cache_prepare.add_argument("--root", type=Path, required=True)
    cache_verify = subparsers.add_parser("release-cache-verify")
    cache_verify.add_argument("--cache", type=Path, required=True)
    cache_verify.add_argument("--decision", type=Path, required=True)
    cache_verify.add_argument("--flavor", required=True)
    cache_verify.add_argument("--key", required=True)
    cache_verify.add_argument("--root", type=Path, required=True)
    delete_cache = subparsers.add_parser("release-cache-delete")
    delete_cache.add_argument("--v2-key", required=True)
    delete_cache.add_argument("--v3-key", required=True)
    args = parser.parse_args()

    if args.command == "lifecycle-gate":
        lifecycle_gate()
    elif args.command == "run-identity":
        run_identity(args.role)
    elif args.command == "export-lifecycle":
        export_lifecycle_outputs(
            args.decision,
            Path(required_environment("GITHUB_OUTPUT")),
            repository_root=args.root,
            event=required_environment("GITHUB_EVENT_NAME"),
            workflow_run_id=required_environment("GITHUB_RUN_ID"),
            run_attempt=required_environment("GITHUB_RUN_ATTEMPT"),
        )
        print("PASS typed lifecycle outputs exported")
    elif args.command == "qualification-decision":
        retention_max_bytes = None
        if args.retention_mode == "series-size":
            if not args.retention_max_bytes or not args.retention_max_bytes.isdecimal():
                raise ValueError("size retention requires a positive byte limit")
            retention_max_bytes = int(args.retention_max_bytes)
        elif args.retention_max_bytes:
            raise ValueError("series retention does not accept a byte limit")
        prepare_pull_request_qualification(
            args.source,
            args.decision,
            repository_root=args.root,
            epoch=args.epoch,
            dkc_revision=args.dkc_revision,
            lto_mode=cast(LtoMode, args.lto_mode),
            retention_mode=cast(RetentionMode, args.retention_mode),
            retention_max_bytes=retention_max_bytes,
        )
        print("PASS pull-request build qualification prepared")
    elif args.command == "export-source":
        export_source_environment(
            args.source, Path(required_environment("GITHUB_ENV"))
        )
        print("PASS authenticated source environment exported")
    elif args.command == "export-image-bundle":
        export_image_bundle(args.input, Path(required_environment("GITHUB_OUTPUT")))
        print("PASS immutable image bundle outputs exported")
    elif args.command == "terminal-result":
        require_terminal_result(
            decision=args.decision,
            decision_result=args.decision_result,
            final_result=args.final_result,
        )
        print("PASS workflow reached the required terminal state")
    elif args.command == "release-cache-prepare":
        prepare_release_cache(
            args.cache,
            flavor_result=args.flavor_result,
            selftest_result=args.selftest_result,
            qemu_result=args.qemu_result,
            decision_root=args.decision,
            flavor=args.flavor,
            build_image=args.build_image,
            toolbox_image=args.toolbox_image,
            expected_key=args.key,
            repository_root=args.root,
        )
        print(f"PASS accepted release cache prepared for {args.flavor}")
    elif args.command == "release-cache-verify":
        verify_release_cache(
            args.cache,
            decision_root=args.decision,
            flavor=args.flavor,
            expected_key=args.key,
            repository_root=args.root,
        )
        print(f"PASS accepted release cache verified for {args.flavor}")
    elif args.command == "release-cache-delete":
        removed = delete_release_caches(
            repository=required_environment("GITHUB_REPOSITORY"),
            selected_ref=required_environment("GITHUB_REF"),
            keys=(args.v2_key, args.v3_key),
            token=required_environment("GITHUB_TOKEN"),
        )
        print(f"PASS removed {removed} accepted release cache entries")
    else:
        raise AssertionError("unhandled GitHub command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
