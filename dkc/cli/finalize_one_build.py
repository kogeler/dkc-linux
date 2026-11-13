"""Final flavor-result checks for finalize-one-build.sh."""

from __future__ import annotations

import hashlib
import json
import lzma
import pathlib
import sys

from dkc.tarmetadata import (
    require_package_archive_evidence,
    require_source_archive_evidence,
)


def check_attestation(arguments: list[str]) -> None:
    """Require the attestation, audits, replay bundle, and gate results to agree."""
    (
        attestation_path,
        kbuild_path,
        simd_path,
        replay_path,
        gates_path,
        identity_path,
        flavor,
    ) = arguments
    identity = json.loads(pathlib.Path(identity_path).read_text(encoding="utf-8"))
    attestation = json.loads(pathlib.Path(attestation_path).read_text(encoding="utf-8"))
    replay = json.loads(pathlib.Path(replay_path).read_text(encoding="utf-8"))

    def record(path: pathlib.Path) -> dict[str, object]:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"size": path.stat().st_size, "sha256": digest.hexdigest()}

    expected_krel = identity.get("kernel_releases", {}).get(flavor)
    expected_lto = identity.get("lto_mode")
    packages = attestation.get("packages")
    if (
        attestation.get("status") != "PASS"
        or attestation.get("flavor") != flavor
        or attestation.get("kernel_release") != expected_krel
        or attestation.get("lto_mode") != expected_lto
        or not isinstance(packages, dict)
    ):
        raise SystemExit("build attestation identity/status did not pass")
    try:
        require_package_archive_evidence(
            attestation.get("package_archive_metadata"),
            packages,
            identity.get("publication_source_date_epoch"),
            "build attestation package archive evidence",
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    if (
        replay.get("status") != "COMPLETE"
        or replay.get("llvm_major") != attestation.get("llvm_major")
        or replay.get("original_vmlinux", {}).get("sha256")
        != attestation.get("vmlinux_sha256")
        or replay.get("schema_version") != 2
        or replay.get("executable_sections_comparison") != "PASS"
    ):
        raise SystemExit(
            "attestation replay bundle does not match the accepted vmlinux"
        )
    replay_root = pathlib.Path(replay_path).parent
    evidence_root = replay_root.parent
    for key, path in (
        ("vmlinux_zst", replay_root / "vmlinux.zst"),
        ("system_map", replay_root / "System.map"),
        ("config", replay_root / "config"),
        ("executable_sections", replay_root / "executable-sections.json"),
        ("kbuild_commands", evidence_root / "kbuild-commands.tsv.xz"),
        ("build_tree_inventory", replay_root / "build-tree-inventory.json"),
    ):
        if replay.get(key) != record(path):
            raise SystemExit(f"attestation replay member differs: {key}")
    executable_sections = json.loads(
        (replay_root / "executable-sections.json").read_text(encoding="utf-8")
    )
    if (
        executable_sections.get("schema_version") != 1
        or executable_sections.get("status") != "PASS"
        or executable_sections.get("llvm_major") != attestation.get("llvm_major")
        or not isinstance(executable_sections.get("sections"), list)
        or not executable_sections["sections"]
    ):
        raise SystemExit("executable-section replay verification did not pass")
    gates = {}
    for line in pathlib.Path(gates_path).read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in gates:
            raise SystemExit("post-build gate result is malformed")
        gates[key] = value
    if gates != {
        "package_attestation_rc": "0",
        "kbuild_audit_rc": "0",
        "simd_audit_rc": "0",
        "lintian_rc": "0",
    }:
        raise SystemExit(f"one or more post-build gates did not pass: {gates!r}")
    for audit_path, label in (
        (kbuild_path, "Kbuild command"),
        (simd_path, "kernel SIMD"),
    ):
        report = json.loads(pathlib.Path(audit_path).read_text(encoding="utf-8"))
        if report.get("status") != "PASS":
            raise SystemExit(f"{label} audit did not pass")
        if report.get("lto_mode") != expected_lto:
            raise SystemExit(f"{label} audit has a different LTO mode")


def check_source_package(arguments: list[str]) -> None:
    """Require the exported source package to match its accepted report."""
    report_path, manifest_path, bundle_path, identity_path = map(
        pathlib.Path, arguments
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if (
        report.get("status") != "PASS"
        or report.get("reconstruction") != "PASS"
        or report.get("package") != "dkc-linux"
        or report.get("version") != identity.get("package_version")
        or report.get("build_input_digest") != identity.get("build_input_digest")
    ):
        raise SystemExit("source-package identity or reconstruction did not pass")
    try:
        require_source_archive_evidence(
            report.get("debian_archive_metadata"),
            identity.get("publication_source_date_epoch"),
            "source package archive evidence",
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    files = report.get("files")
    if not isinstance(files, list) or len(files) != 5:
        raise SystemExit("source-package report lacks the exact five-file bundle")
    actual_names = {path.name for path in bundle_path.iterdir() if path.is_file()}
    reported_names = {item.get("name") for item in files if isinstance(item, dict)}
    if actual_names != reported_names:
        raise SystemExit("source-package directory and report file sets differ")
    for item in files:
        if not isinstance(item, dict):
            raise SystemExit("malformed source-package file record")
        path = bundle_path / str(item.get("name"))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item.get("sha256") or path.stat().st_size != item.get("size"):
            raise SystemExit(f"source-package member differs: {path.name}")
    manifest = lzma.decompress(manifest_path.read_bytes())
    if hashlib.sha256(manifest).hexdigest() != report.get(
        "source_tree_manifest_sha256"
    ):
        raise SystemExit(
            "compressed reconstructed-source manifest differs from its report"
        )
    if len(manifest.splitlines()) != report.get("source_tree_entries"):
        raise SystemExit("reconstructed-source manifest entry count differs")


COMMANDS = {
    "attestation": check_attestation,
    "source-package": check_source_package,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        commands = "|".join(COMMANDS)
        raise SystemExit(
            f"usage: python3 -m dkc.cli.finalize_one_build {{{commands}}} ..."
        )
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
