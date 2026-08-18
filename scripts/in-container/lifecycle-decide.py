#!/usr/bin/env python3
"""Combine authenticated source and storage state into one typed decision."""

from __future__ import annotations

import argparse
import hashlib
import pathlib
from datetime import datetime, timezone

from dkc.buildpolicy import build_policy_digest
from dkc.handoffs import (
    load_authoritative_state_handoff,
    load_source_handoff,
)
from dkc.lifecycle import decide
from dkc.release_gate import discovery_decision_outputs
from dkc.schema import validate
from dkc.serialize import dumps, parse_boolean_text


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=pathlib.Path, required=True)
    parser.add_argument("--state", type=pathlib.Path, required=True)
    parser.add_argument("--keyring", type=pathlib.Path, required=True)
    parser.add_argument("--signing-subkeys", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--bootstrap-allowed", type=parse_boolean_text, required=True)
    parser.add_argument("--dkc-revision", type=int, required=True)
    parser.add_argument("--lto-mode", choices=("none", "thin", "full"), required=True)
    parser.add_argument("--retention-mode", choices=("series", "series-size"), required=True)
    parser.add_argument("--retention-max-bytes", required=True)
    args = parser.parse_args()
    if args.output.exists() or args.epoch < 1:
        raise SystemExit("invalid or pre-existing lifecycle decision output")
    if args.retention_mode == "series":
        if args.retention_max_bytes:
            raise SystemExit("series retention does not accept a byte limit")
        retention_max_bytes = None
    else:
        if not args.retention_max_bytes.isdecimal() or int(args.retention_max_bytes) < 1:
            raise SystemExit("size retention requires a positive byte limit")
        retention_max_bytes = int(args.retention_max_bytes)
    source = load_source_handoff(args.source)
    dsc = source.get("dsc")
    if not isinstance(dsc, dict) or not isinstance(dsc.get("sha256"), str):
        raise ValueError("source inventory lacks its descriptor hash")
    current_state = load_authoritative_state_handoff(
        args.state,
        keyring=args.keyring,
        signing_subkeys=args.signing_subkeys,
    )
    result = decide(
        source_version=str(source["source_version"]),
        source_dsc_sha256=dsc["sha256"],
        dkc_revision=args.dkc_revision,
        build_policy_sha256=build_policy_digest(pathlib.Path.cwd()),
        lto_mode=args.lto_mode,
        retention_mode=args.retention_mode,
        retention_max_bytes=retention_max_bytes,
        now=datetime.fromtimestamp(args.epoch, timezone.utc),
        state=current_state,
        state_read_succeeded=True,
        bootstrap_allowed=args.bootstrap_allowed,
    )
    validate("discovery-decision", result.to_dict())
    args.output.mkdir(parents=True)
    (args.output / "decision.json").write_text(
        dumps(result.to_dict()), encoding="utf-8"
    )
    outputs = discovery_decision_outputs(result)
    (args.output / "outputs.env").write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(outputs.items())),
        encoding="utf-8",
    )
    (args.output / "result.env").write_text(
        f"status=PASS\nlifecycle_decision={result.decision}\n", encoding="utf-8"
    )
    checksums = []
    for path in sorted(args.output.iterdir()):
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (args.output / "evidence.sha256").write_text(
        "\n".join(checksums) + "\n", encoding="utf-8"
    )
    print(f"PASS lifecycle decision: {result.decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
