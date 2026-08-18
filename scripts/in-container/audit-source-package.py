#!/usr/bin/env python3
"""Audit one final source bundle and its reconstructed source tree."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def manifest_difference(prepared: str, reconstructed: str) -> dict[str, object]:
    def records(value: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in value.splitlines():
            fields = line.split("\t")
            if fields[:1] == ["f"] and len(fields) == 5:
                path = fields[4]
            elif fields[:1] == ["l"] and len(fields) == 4:
                path = fields[2]
            else:
                raise ValueError("source-tree manifest has a malformed record")
            if not path or path in result:
                raise ValueError("source-tree manifest has an invalid path inventory")
            result[path] = line
        return result

    expected = records(prepared)
    actual = records(reconstructed)
    missing = sorted(set(expected) - set(actual))
    unexpected = sorted(set(actual) - set(expected))
    changed = sorted(
        path for path in set(expected) & set(actual) if expected[path] != actual[path]
    )
    limit = 20
    return {
        "changed_count": len(changed),
        "changed_sample": [
            {"path": path, "prepared": expected[path], "reconstructed": actual[path]}
            for path in changed[:limit]
        ],
        "missing_count": len(missing),
        "missing_sample": missing[:limit],
        "unexpected_count": len(unexpected),
        "unexpected_sample": unexpected[:limit],
    }


def main() -> int:
    if len(sys.argv) != 7:
        print(
            "usage: audit-source-package.py <repo> <bundle> <identity> "
            "<prepared-tree> <reconstructed-tree> <evidence>",
            file=sys.stderr,
        )
        return 2
    repo, bundle_root, identity_path, prepared, reconstructed, evidence = (
        pathlib.Path(value).resolve() for value in sys.argv[1:]
    )
    sys.path.insert(0, str(repo))
    from dkc.debver import DebianVersion  # noqa: PLC0415
    from dkc.serialize import dumps  # noqa: PLC0415
    from dkc.sourcepackage import (  # noqa: PLC0415
        build_tree_manifest,
        validate_source_bundle,
    )

    identity_bytes = identity_path.read_bytes()
    identity = json.loads(identity_bytes)
    if identity.get("source_package") != "dkc-linux":
        raise SystemExit("foreign source identity")
    version = identity.get("package_version")
    debian_version = identity.get("debian_source_version")
    package_names = identity.get("package_names")
    if (
        not isinstance(version, str)
        or not isinstance(debian_version, str)
        or not isinstance(package_names, dict)
    ):
        raise SystemExit("malformed source identity")
    binary_packages = package_names.get("versioned", []) + package_names.get("meta", [])
    if not all(isinstance(item, str) for item in binary_packages):
        raise SystemExit("malformed binary package identity")
    source_bundle = validate_source_bundle(
        bundle_root,
        package="dkc-linux",
        version=version,
        upstream_version=DebianVersion.parse(debian_version).upstream_release,
        expected_binary_packages=binary_packages,
    )

    embedded_identity = reconstructed / "debian/dkc/publication-identity.json"
    if embedded_identity.read_bytes() != identity_bytes:
        raise SystemExit("reconstructed source embeds a different publication identity")
    prepared_manifest = build_tree_manifest(prepared)
    reconstructed_manifest = build_tree_manifest(reconstructed)
    if prepared_manifest != reconstructed_manifest:
        evidence.mkdir(parents=True, exist_ok=True)
        difference = manifest_difference(prepared_manifest, reconstructed_manifest)
        (evidence / "source-tree-difference.json").write_text(
            dumps(difference), encoding="utf-8"
        )
        raise SystemExit("dpkg-source reconstruction differs from the prepared final source")

    evidence.mkdir(parents=True, exist_ok=True)
    manifest_path = evidence / "source-tree.manifest"
    manifest_path.write_text(prepared_manifest, encoding="utf-8")
    report = source_bundle.to_dict()
    report.update(
        {
            "build_input_digest": identity.get("build_input_digest"),
            "reconstruction": "PASS",
            "source_tree_entries": len(prepared_manifest.splitlines()),
            "source_tree_manifest_sha256": sha256_bytes(prepared_manifest.encode()),
            "status": "PASS",
        }
    )
    (evidence / "source-package.json").write_text(dumps(report), encoding="utf-8")
    print(
        f"source package PASS: {len(source_bundle.files)} files, "
        f"{len(prepared_manifest.splitlines())} source entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
