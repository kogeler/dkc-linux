"""VM preparation failures retain their diagnostics before scratch cleanup."""

from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import subprocess
import tempfile
from collections.abc import Iterator

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def tmp_path() -> Iterator[pathlib.Path]:
    # The toolbox deliberately mounts /tmp noexec. Stub host programs need
    # the executable work filesystem, just like the real VM input preparer.
    scratch = ROOT / ".dkc-run"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="qemu-fixture-", dir=scratch) as directory:
        yield pathlib.Path(directory)


def _executable(path: pathlib.Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + contents)
    path.chmod(0o755)


def test_explicit_preparation_failure_exports_log_before_cleanup(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts/lib").mkdir(parents=True)
    for relative in ("scripts/qemu-boot.sh", "scripts/lib/common.sh"):
        shutil.copy2(ROOT / relative, repo / relative)
    _executable(repo / "scripts/qemu-preflight.sh", """
cat >"$DKC_RUN_DIR/qemu-preflight.env" <<EOF
accelerator=kvm
machine=q35
cpu_v2=Nehalem-v1
cpu_v3=Haswell-v2
cpu_v4=EPYC-Genoa-v1
EOF
""")
    tools = tmp_path / "bin"
    _executable(tools / "podman", """
case "$1" in
    info) echo true ;;
    image) exit 0 ;;
    run) echo 'distinct VM preparation diagnostic' >&2; exit 42 ;;
    *) exit 1 ;;
esac
""")
    for command in ("git", "qemu-system-x86_64", "qemu-img"):
        _executable(tools / command, "exit 0\n")

    cache = tmp_path / "cache"
    (cache / "qemu").mkdir(parents=True)
    base = cache / "qemu/base.qcow2"
    base.write_bytes(b"immutable image fixture")
    base.chmod(0o444)
    config = repo / "config"
    config.mkdir()
    image_config = config / "image.env"
    image_config.write_text(
        "DKC_QEMU_IMAGE_URL=https://example.invalid/base.qcow2\n"
        "DKC_QEMU_IMAGE_FILENAME=base.qcow2\n"
        f"DKC_QEMU_IMAGE_SHA512={hashlib.sha512(base.read_bytes()).hexdigest()}\n"
    )
    cpu_config = config / "cpus.env"
    cpu_config.write_text("")
    flavor = tmp_path / "flavor"
    (flavor / "artifacts").mkdir(parents=True)
    (flavor / "evidence").mkdir()
    (flavor / "evidence/publication-identity.json").write_text('{"lto_mode":"thin"}')
    selftest = tmp_path / "selftest"
    (selftest / "evidence").mkdir(parents=True)
    run_id = "20000101T000000Z-00000000"
    environment = {
        **os.environ,
        "PATH": str(tools) + os.pathsep + os.environ["PATH"],
        "DKC_ROOT": str(repo),
        "DKC_CACHE_DIR": str(cache),
        "DKC_RUN_ID": run_id,
    }
    result = subprocess.run(
        [
            "bash", str(repo / "scripts/qemu-boot.sh"), "fixture-toolbox",
            str(image_config), str(cpu_config), "v2", "kvm", "600", "2048", "1",
            str(flavor), str(selftest), "0",
        ],
        env=environment, text=True, capture_output=True, check=False, timeout=30,
    )
    assert result.returncode != 0
    assert "QEMU input preparation failed" in result.stderr, result.stderr
    evidence = repo / "out/qemu-boot" / run_id / "evidence"
    assert "distinct VM preparation diagnostic" in (
        evidence / "evidence-preparation.log"
    ).read_text()
    assert "status=FAIL\n" in (evidence / "result.env").read_text()
    assert not (repo / ".dkc-run" / run_id / "qemu-boot").exists()
    assert base.read_bytes() == b"immutable image fixture"
