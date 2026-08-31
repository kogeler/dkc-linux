from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import pathlib
import subprocess
import tarfile
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest

from dkc.sourcepackage import (
    build_tree_manifest,
    parse_checksums_sha256,
    parse_deb822,
    validate_source_bundle,
)
from dkc.tarmetadata import normalize_tree_metadata, validate_tar_stream


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


def test_public_source_metadata_pins_the_complete_tree(
    tmp_path: pathlib.Path,
) -> None:
    epoch = 1_760_000_000
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
    os.utime(plain, (epoch - 20, epoch - 20))
    os.utime(executable, (epoch + 20, epoch + 20))

    normalize_tree_metadata(tmp_path, epoch)

    for path in (tmp_path, directory, plain, executable, link):
        assert path.lstat().st_mtime_ns == epoch * 1_000_000_000
    assert directory.stat().st_mode & 0o777 == 0o755
    assert plain.stat().st_mode & 0o777 == 0o644
    assert executable.stat().st_mode & 0o777 == 0o755


def test_public_source_metadata_rejects_special_files(tmp_path: pathlib.Path) -> None:
    os.mkfifo(tmp_path / "fifo")
    with pytest.raises(ValueError, match="special entry"):
        normalize_tree_metadata(tmp_path, 1_760_000_000)


def _metadata_tar(*, epoch: int, changed_mtime: int | None = None) -> bytes:
    output = io.BytesIO()
    with tarfile.open(
        fileobj=output, mode="w:xz", format=tarfile.GNU_FORMAT
    ) as archive:
        directory = tarfile.TarInfo("debian")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.mtime = epoch
        directory.uid = directory.gid = 0
        directory.uname = directory.gname = ""
        archive.addfile(directory)
        body = b"Source: dkc-linux\n"
        control = tarfile.TarInfo("debian/control")
        control.mode = 0o644
        control.mtime = epoch if changed_mtime is None else changed_mtime
        control.size = len(body)
        control.uid = control.gid = 0
        control.uname = control.gname = ""
        archive.addfile(control, io.BytesIO(body))
    return output.getvalue()


def test_source_tar_requires_exact_normalized_metadata() -> None:
    epoch = 1_760_000_000
    metadata = validate_tar_stream(
        io.BytesIO(_metadata_tar(epoch=epoch)),
        label="fixture source archive",
        epoch=epoch,
        exact_mtime=True,
        expected_prefix="debian",
        require_sorted=True,
        normalized_modes=True,
    )
    assert metadata.to_dict() == {
        "member_count": 2,
        "minimum_mtime": epoch,
        "maximum_mtime": epoch,
    }

    with pytest.raises(ValueError, match="mtime"):
        validate_tar_stream(
            io.BytesIO(_metadata_tar(epoch=epoch, changed_mtime=epoch - 1)),
            label="fixture source archive",
            epoch=epoch,
            exact_mtime=True,
            expected_prefix="debian",
            require_sorted=True,
            normalized_modes=True,
        )


def test_source_tar_accepts_sorted_recursive_directory_traversal() -> None:
    epoch = 1_760_000_000
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:xz", format=tarfile.GNU_FORMAT) as archive:
        for name, kind in (
            ("debian", tarfile.DIRTYPE),
            ("debian/config", tarfile.DIRTYPE),
            ("debian/config/value", tarfile.REGTYPE),
            ("debian/config.local", tarfile.REGTYPE),
        ):
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
            member.mtime = epoch
            member.uid = member.gid = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO() if kind == tarfile.REGTYPE else None)

    validate_tar_stream(
        io.BytesIO(output.getvalue()),
        label="recursive source archive",
        epoch=epoch,
        exact_mtime=True,
        expected_prefix="debian",
        require_sorted=True,
        normalized_modes=True,
    )


def test_binary_tar_requires_the_exact_publication_epoch() -> None:
    epoch = 1_760_000_000
    validate_tar_stream(
        io.BytesIO(_metadata_tar(epoch=epoch)),
        label="fixture binary archive",
        epoch=epoch,
        exact_mtime=True,
        normalized_modes=True,
    )
    for changed in (epoch - 1, epoch + 1):
        with pytest.raises(ValueError, match="differs"):
            validate_tar_stream(
                io.BytesIO(_metadata_tar(epoch=epoch, changed_mtime=changed)),
                label="fixture binary archive",
                epoch=epoch,
                exact_mtime=True,
                normalized_modes=True,
            )


def test_dpkg_source_preserves_the_exact_normalized_archive_contract(
    tmp_path: pathlib.Path,
) -> None:
    epoch = 1_760_000_000
    source = tmp_path / "fixture-1.0"
    debian = source / "debian"
    (debian / "source").mkdir(parents=True)
    (source / "README").write_text("fixture\n", encoding="utf-8")
    (debian / "source/format").write_text("3.0 (quilt)\n", encoding="utf-8")
    (debian / "control").write_text(
        "Source: fixture\nBuild-Depends: debhelper-compat (= 13)\n\n"
        "Package: fixture\nArchitecture: all\nDescription: fixture\n test\n",
        encoding="utf-8",
    )
    (debian / "changelog").write_text(
        "fixture (1.0-1) unstable; urgency=medium\n\n"
        "  * Test deterministic source metadata.\n\n"
        " -- Fixture Builder <fixture@example.invalid>  "
        f"{format_datetime(datetime.fromtimestamp(epoch, UTC))}\n",
        encoding="utf-8",
    )
    rules = debian / "rules"
    rules.write_text("#!/usr/bin/make -f\n%:\n\tdh $@\n", encoding="utf-8")
    rules.chmod(0o755)

    orig = tmp_path / "fixture_1.0.orig.tar.xz"
    with tarfile.open(orig, mode="w:xz", format=tarfile.GNU_FORMAT) as archive:
        root = tarfile.TarInfo("fixture-1.0")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        root.mtime = epoch
        root.uid = root.gid = 0
        root.uname = root.gname = ""
        archive.addfile(root)
        body = b"fixture\n"
        readme = tarfile.TarInfo("fixture-1.0/README")
        readme.mode = 0o644
        readme.mtime = epoch
        readme.size = len(body)
        readme.uid = readme.gid = 0
        readme.uname = readme.gname = ""
        archive.addfile(readme, io.BytesIO(body))

    normalize_tree_metadata(source, epoch)
    result = subprocess.run(
        ["dpkg-source", "--build", source],
        cwd=tmp_path,
        env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "TZ": "UTC"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    metadata = validate_tar_stream(
        io.BytesIO((tmp_path / "fixture_1.0-1.debian.tar.xz").read_bytes()),
        label="dpkg-source fixture archive",
        epoch=epoch,
        exact_mtime=True,
        expected_prefix="debian",
        require_sorted=True,
        normalized_modes=True,
    )
    assert metadata.member_count >= 5


def test_dpkg_deb_preserves_the_exact_normalized_archive_contract(
    tmp_path: pathlib.Path,
) -> None:
    epoch = 1_760_000_000
    package = tmp_path / "package"
    (package / "DEBIAN").mkdir(parents=True)
    (package / "usr/bin").mkdir(parents=True)
    (package / "DEBIAN/control").write_text(
        "Package: fixture\nVersion: 1.0-1\nArchitecture: all\n"
        "Maintainer: Fixture Builder <fixture@example.invalid>\n"
        "Description: fixture\n",
        encoding="utf-8",
    )
    executable = package / "usr/bin/fixture"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    normalize_tree_metadata(package, epoch)

    output = tmp_path / "fixture.deb"
    result = subprocess.run(
        ["dpkg-deb", "--build", "--root-owner-group", package, output],
        env={**os.environ, "SOURCE_DATE_EPOCH": str(epoch), "TZ": "UTC"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for option in ("--ctrl-tarfile", "--fsys-tarfile"):
        archive = subprocess.run(
            ["dpkg-deb", option, output], capture_output=True, check=False
        )
        assert archive.returncode == 0, archive.stderr.decode(errors="replace")
        metadata = validate_tar_stream(
            io.BytesIO(archive.stdout),
            label=f"dpkg-deb fixture {option}",
            epoch=epoch,
            exact_mtime=True,
            normalized_modes=True,
        )
        assert metadata.member_count >= 2
