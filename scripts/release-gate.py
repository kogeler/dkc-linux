#!/usr/bin/env python3
"""Command-line entry points for provider-neutral release handoff gates."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dkc.release_gate import (
    require_publication_matches_decision,
    require_state_generation,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    publication = subparsers.add_parser("publication-decision")
    publication.add_argument("--decision", type=Path, required=True)
    publication.add_argument("--repository-result", type=Path, required=True)

    state = subparsers.add_parser("state-generation")
    state.add_argument("--state", type=Path, required=True)
    state.add_argument("--expected", type=int, required=True)
    state.add_argument("--keyring", type=Path, required=True)
    state.add_argument("--signing-subkeys", type=Path, required=True)
    args = parser.parse_args()

    if args.command == "publication-decision":
        require_publication_matches_decision(args.decision, args.repository_result)
        print("PASS verified repository matches its lifecycle decision")
    elif args.command == "state-generation":
        require_state_generation(
            args.state,
            args.expected,
            keyring=args.keyring,
            signing_subkeys=args.signing_subkeys,
        )
        print("PASS authoritative state has the intended generation")
    else:
        raise AssertionError("unhandled release gate command")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
