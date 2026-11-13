"""Selftest-bundle checks for build-kselftest-flavor.sh."""

from __future__ import annotations

import hashlib
import json
import pathlib
import re
import shlex
import sys

from dkc.debver import DebianVersion
from dkc.sourcepackage import validate_source_bundle
from dkc.tarmetadata import (
    require_package_archive_evidence,
    require_source_archive_evidence,
)


def check_inputs(arguments: list[str]) -> None:
    """Require the accepted kernel, source package, and selftest profile."""
    flavor_text, flavor, llvm_text, kind, profile_text = arguments
    flavor_root = pathlib.Path(flavor_text)
    profile_path = pathlib.Path(profile_text)
    identity = json.loads(
        (flavor_root / "evidence/publication-identity.json").read_text(encoding="utf-8")
    )
    attestation = json.loads(
        (flavor_root / "evidence/attestation.json").read_text(encoding="utf-8")
    )
    source_report = json.loads(
        (flavor_root / "evidence/source-package/source-package.json").read_text(
            encoding="utf-8"
        )
    )
    digest = identity.get("build_input_digest")
    kernel_release = identity.get("kernel_releases", {}).get(flavor)
    publication_epoch = identity.get("publication_source_date_epoch")
    if (
        not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
        or source_report.get("build_input_digest") != digest
    ):
        raise SystemExit("kernel and source evidence have different build identities")
    try:
        require_source_archive_evidence(
            source_report.get("debian_archive_metadata"),
            publication_epoch,
            "source package archive evidence",
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (
        attestation.get("schema_version") != 2
        or attestation.get("status") != "PASS"
        or attestation.get("flavor") != flavor
        or attestation.get("kernel_release") != kernel_release
        or attestation.get("lto_mode") != identity.get("lto_mode")
        or attestation.get("llvm_major") != int(llvm_text)
    ):
        raise SystemExit("kernel attestation does not match the requested test build")
    attested_packages = attestation.get("packages")
    if not isinstance(attested_packages, dict):
        raise SystemExit("kernel attestation has no package inventory")
    try:
        require_package_archive_evidence(
            attestation.get("package_archive_metadata"),
            attested_packages,
            publication_epoch,
            "kernel package archive evidence",
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    package_names = identity.get("package_names", {})
    expected_binaries = set(package_names.get("versioned", [])) | set(
        package_names.get("meta", [])
    )
    bundle = validate_source_bundle(
        flavor_root / "source",
        package="dkc-linux",
        version=str(identity.get("package_version")),
        upstream_version=DebianVersion.parse(
            str(identity.get("debian_source_version"))
        ).upstream_release,
        expected_binary_packages=expected_binaries,
    )
    if any(source_report.get(key) != value for key, value in bundle.to_dict().items()):
        raise SystemExit("physical source bundle differs from its accepted report")
    profile_values: dict[str, str] = {}
    for raw_line in profile_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        parsed = shlex.split(value) if separator else []
        if len(parsed) != 1 or key in profile_values:
            raise SystemExit("malformed kselftest profile")
        profile_values[key] = parsed[0]
    if profile_values.get("DKC_KSELFTEST_PROFILE_KIND") != kind:
        raise SystemExit("kselftest output kind differs from the selected profile")


def bind_report(arguments: list[str]) -> None:
    """Bind the selftest build report to the shipped kernel and its source patches."""
    report_path, attestation_path, source_report_path, patch_manifest, patch_results = (
        map(pathlib.Path, arguments)
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    if report.get("kernel_config_sha256") != attestation.get("shipped_config_sha256"):
        raise SystemExit(
            "selftest bundle was not built against the shipped configuration"
        )
    report["source_package_report_sha256"] = hashlib.sha256(
        source_report_path.read_bytes()
    ).hexdigest()
    report["source_tree_manifest_sha256"] = source_report.get(
        "source_tree_manifest_sha256"
    )
    report["source_patch_manifest_sha256"] = hashlib.sha256(
        patch_manifest.read_bytes()
    ).hexdigest()
    report["source_patch_count"] = len(
        patch_manifest.read_text(encoding="utf-8").splitlines()
    )
    report["source_patch_results"] = dict(
        line.split("\t", 1)
        for line in patch_results.read_text(encoding="utf-8").splitlines()
    )
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


COMMANDS = {
    "inputs": check_inputs,
    "bind-report": bind_report,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        commands = "|".join(COMMANDS)
        raise SystemExit(
            f"usage: python3 -m dkc.cli.build_kselftest_flavor {{{commands}}} ..."
        )
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
