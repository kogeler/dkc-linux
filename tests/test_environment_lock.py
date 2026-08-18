from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/in-container/lock-build-environment.py"
SPEC = importlib.util.spec_from_file_location("lock_build_environment", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_retained_archives_are_filtered_hashed_and_sorted(tmp_path: Path) -> None:
    packages = tmp_path / "packages.tsv"
    packages.write_text("zlib1g:amd64\t1.3\tamd64\nlibc6:amd64\t2.41\tamd64\n")
    archives = tmp_path / "archives"
    archives.mkdir()
    libc = archives / "libc6_2.41_amd64.deb"
    zlib = archives / "zlib1g_1.3_amd64.deb"
    stale = archives / "zlib1g_1.2_amd64.deb"
    libc.write_bytes(b"libc")
    zlib.write_bytes(b"zlib")
    stale.write_bytes(b"stale")
    identities = {
        libc.name: ("libc6:amd64", "2.41", "amd64"),
        zlib.name: ("zlib1g:amd64", "1.3", "amd64"),
        stale.name: ("zlib1g:amd64", "1.2", "amd64"),
    }

    rows = MODULE.retained_archive_rows(
        MODULE.read_installed(packages),
        archives,
        identity_reader=lambda path: identities[path.name],
    )
    assert [row[0] for row in rows] == ["libc6:amd64", "zlib1g:amd64"]
    assert rows[0][3] == "image-cache://libc6_2.41_amd64.deb"
    assert rows[0][4] == "4"
    assert rows[0][5] == hashlib.sha256(b"libc").hexdigest()


def test_environment_lock_rejects_duplicates_and_empty_archives(tmp_path: Path) -> None:
    installed = {("pkg:amd64", "1", "amd64")}
    archives = tmp_path / "archives"
    archives.mkdir()
    with pytest.raises(ValueError, match="no retained"):
        MODULE.retained_archive_rows(installed, archives)

    (archives / "pkg_1_amd64.deb").write_bytes(b"one")
    (archives / "pkg_1+b1_amd64.deb").write_bytes(b"two")
    with pytest.raises(ValueError, match="duplicate"):
        MODULE.retained_archive_rows(
            installed,
            archives,
            identity_reader=lambda _path: ("pkg:amd64", "1", "amd64"),
        )
