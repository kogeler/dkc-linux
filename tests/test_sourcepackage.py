from __future__ import annotations

import hashlib
import importlib.util
import pathlib

import pytest

from dkc.sourcepackage import (
    build_tree_manifest,
    parse_checksums_sha256,
    parse_deb822,
    validate_source_bundle,
)


ROOT = pathlib.Path(__file__).resolve().parent.parent
AUDIT_SPEC = importlib.util.spec_from_file_location(
    "audit_source_package", ROOT / "scripts/in-container/audit-source-package.py"
)
assert AUDIT_SPEC and AUDIT_SPEC.loader
audit_source = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(audit_source)
PREPARE_SPEC = importlib.util.spec_from_file_location(
    "prepare_source_tree", ROOT / "scripts/in-container/prepare-source-tree.py"
)
assert PREPARE_SPEC and PREPARE_SPEC.loader
prepare_source = importlib.util.module_from_spec(PREPARE_SPEC)
PREPARE_SPEC.loader.exec_module(prepare_source)


PACKAGE = "dkc-linux"
VERSION = "7.1.7-1+dkc13.1"
UPSTREAM = "7.1.7"
BINARY = ("dkc-linux-image-v2-amd64", "dkc-linux-image-v3-amd64")


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checksum(path: pathlib.Path) -> str:
    return f" {_digest(path)} {path.stat().st_size} {path.name}"


def _bundle(root: pathlib.Path) -> None:
    prefix = f"{PACKAGE}_{VERSION}"
    orig = root / f"{PACKAGE}_{UPSTREAM}.orig.tar.xz"
    debian = root / f"{prefix}.debian.tar.xz"
    orig.write_bytes(b"orig")
    debian.write_bytes(b"debian")
    dsc = root / f"{prefix}.dsc"
    dsc.write_text(
        "\n".join(
            (
                "Format: 3.0 (quilt)",
                f"Source: {PACKAGE}",
                f"Binary: {', '.join(BINARY)}",
                f"Version: {VERSION}",
                "Checksums-Sha256:",
                _checksum(orig),
                _checksum(debian),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    buildinfo = root / f"{prefix}_source.buildinfo"
    buildinfo.write_text(
        "\n".join(
            (
                "Format: 1.0",
                f"Source: {PACKAGE}",
                f"Version: {VERSION}",
                "Architecture: source",
                "Checksums-Sha256:",
                _checksum(dsc),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    changes = root / f"{prefix}_source.changes"
    changes.write_text(
        "\n".join(
            (
                "Format: 1.8",
                f"Source: {PACKAGE}",
                f"Version: {VERSION}",
                "Architecture: source",
                "Distribution: trixie",
                "Checksums-Sha256:",
                _checksum(dsc),
                _checksum(orig),
                _checksum(debian),
                _checksum(buildinfo),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def test_strict_deb822_parser() -> None:
    assert parse_deb822("Source: one\nFiles:\n first\n second\n") == {
        "Source": "one",
        "Files": "\nfirst\nsecond",
    }
    with pytest.raises(ValueError, match="duplicate"):
        parse_deb822("Source: one\nSource: two\n")
    with pytest.raises(ValueError, match="paragraph"):
        parse_deb822("Source: one\n\nVersion: two\n")


def test_checksum_parser_rejects_paths_and_duplicates() -> None:
    digest = "a" * 64
    with pytest.raises(ValueError, match="unsafe"):
        parse_checksums_sha256(f"{digest} 1 ../escape", "test")
    with pytest.raises(ValueError, match="duplicate"):
        parse_checksums_sha256(f"{digest} 1 one\n{digest} 1 one", "test")


def test_complete_source_bundle_cross_checks_every_hash(tmp_path: pathlib.Path) -> None:
    _bundle(tmp_path)
    bundle = validate_source_bundle(
        tmp_path,
        package=PACKAGE,
        version=VERSION,
        upstream_version=UPSTREAM,
        expected_binary_packages=BINARY,
    )
    assert len(bundle.files) == 5
    assert bundle.orig == f"{PACKAGE}_{UPSTREAM}.orig.tar.xz"


def test_source_bundle_rejects_a_mutated_member(tmp_path: pathlib.Path) -> None:
    _bundle(tmp_path)
    (tmp_path / f"{PACKAGE}_{UPSTREAM}.orig.tar.xz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="differs"):
        validate_source_bundle(
            tmp_path,
            package=PACKAGE,
            version=VERSION,
            upstream_version=UPSTREAM,
            expected_binary_packages=BINARY,
        )


def test_source_tree_manifest_covers_modes_files_and_links(tmp_path: pathlib.Path) -> None:
    (tmp_path / "dir").mkdir()
    source = tmp_path / "dir/file"
    source.write_text("one\n", encoding="utf-8")
    source.chmod(0o755)
    (tmp_path / "link").symlink_to("dir/file")
    first = build_tree_manifest(tmp_path)
    assert "f\t0755" in first
    assert "l\t0777\tlink\tdir/file" in first
    source.write_text("two\n", encoding="utf-8")
    assert build_tree_manifest(tmp_path) != first


def test_manifest_difference_is_bounded_and_classified() -> None:
    prepared = (
        "f\t0644\t1\ta\tkept\nf\t0644\t1\tb\tmissing\n"
        "l\t0777\tlink\ttarget\n"
    )
    reconstructed = (
        "f\t0755\t1\ta\tkept\nf\t0644\t1\tc\tnew\n"
        "l\t0777\tlink\tother\n"
    )
    difference = audit_source.manifest_difference(prepared, reconstructed)
    assert difference["changed_count"] == 2
    assert difference["missing_sample"] == ["missing"]
    assert difference["unexpected_sample"] == ["new"]


def test_public_source_modes_are_deterministic(tmp_path: pathlib.Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o700)
    plain = directory / "plain"
    plain.write_text("plain\n", encoding="utf-8")
    plain.chmod(0o600)
    executable = directory / "executable"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    link = tmp_path / "link"
    link.symlink_to("directory/plain")

    prepare_source.normalize_public_modes(tmp_path)

    assert directory.stat().st_mode & 0o777 == 0o755
    assert plain.stat().st_mode & 0o777 == 0o644
    assert executable.stat().st_mode & 0o777 == 0o755
    assert link.is_symlink()
