"""Signed-repository client checks for test-signed-repository.sh."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys

from dkc.schema import validate
from dkc.serialize import dumps
from dkc.sourcepackage import build_tree_manifest


def check_state(arguments: list[str]) -> None:
    """Require the signed state pointer, manifests, and repository graph to agree."""

    def sha256(path: pathlib.Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    root = pathlib.Path(arguments[0]).resolve()
    pointer = json.loads(pathlib.Path(arguments[1]).read_text(encoding="utf-8"))
    validate("state-pointer", pointer)
    manifest_path = root / pointer["manifest_key"]
    if (
        not manifest_path.is_file()
        or sha256(manifest_path) != pointer["manifest_sha256"]
    ):
        raise SystemExit("state pointer does not identify the immutable manifest bytes")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate("publication-manifest", manifest)
    if (
        manifest["publication_id"] != pointer["publication_id"]
        or manifest["generation"] != pointer["generation"]
        or (root / "manifest.json").read_bytes() != manifest_path.read_bytes()
    ):
        raise SystemExit(
            "state pointer, immutable manifest, and root manifest disagree"
        )

    inrelease = root / "dists/trixie/InRelease"
    if manifest["apt_metadata"]["inrelease_sha256"] != sha256(inrelease):
        raise SystemExit("manifest does not bind the committed InRelease")
    for relative, expected in manifest["apt_metadata"]["index_hashes"].items():
        if sha256(root / relative) != expected:
            raise SystemExit(f"manifest index hash differs: {relative}")

    artifact_keys = {item["key"] for item in manifest["artifacts"]}
    if artifact_keys != set(manifest["live_objects"]):
        raise SystemExit("local current publication does not mark every artifact live")
    expected_artifacts = {
        path.relative_to(root).as_posix()
        for prefix in ("pool", "dists", "keys")
        for path in (root / prefix).rglob("*")
        if path.is_file()
    }
    if artifact_keys != expected_artifacts:
        raise SystemExit("manifest artifact graph differs from the repository graph")
    for item in manifest["artifacts"]:
        path = root / item["key"]
        if (
            not path.is_file()
            or path.stat().st_size != item["size"]
            or sha256(path) != item["sha256"]
        ):
            raise SystemExit(f"manifest artifact differs: {item['key']}")

    transaction_path = root / (
        f"state/transactions/{manifest['transaction_id']}/record.json"
    )
    transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
    validate("transaction", transaction)
    if (
        transaction["publication_id"] != manifest["publication_id"]
        or transaction["expected_generation"] != manifest["generation"]
        or transaction["intended_inrelease_sha256"] != sha256(inrelease)
    ):
        raise SystemExit("transaction, manifest, and InRelease disagree")


def print_current_version(arguments: list[str]) -> None:
    """Print the one current DKC package version that the signed manifest names."""
    manifest = json.loads(pathlib.Path(arguments[0]).read_text(encoding="utf-8"))
    versions = set(manifest["meta_packages"].values())
    if len(versions) != 1:
        raise SystemExit("signed manifest does not identify one current DKC version")
    print(versions.pop())


def check_source_rebuild(arguments: list[str]) -> None:
    """Require a source-only rebuild to reproduce the reconstructed source tree."""
    original = build_tree_manifest(pathlib.Path(arguments[0]))
    rebuilt = build_tree_manifest(pathlib.Path(arguments[1]))
    if original != rebuilt:
        raise SystemExit("source-only rebuild changed the reconstructed source tree")
    payload = original.encode()
    pathlib.Path(arguments[2]).write_text(
        dumps(
            {
                "source_tree_entries": len(original.splitlines()),
                "source_tree_manifest_sha256": hashlib.sha256(payload).hexdigest(),
                "status": "PASS",
            }
        ),
        encoding="utf-8",
    )


COMMANDS = {
    "state": check_state,
    "current-version": print_current_version,
    "source-rebuild": check_source_rebuild,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        commands = "|".join(COMMANDS)
        raise SystemExit(
            f"usage: python3 -m dkc.cli.signed_repository_client {{{commands}}} ..."
        )
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
