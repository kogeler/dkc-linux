"""Exact dependency-lock normalization for the publication identity."""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "in-container" / "prepare-build-identity.py"
SPEC = importlib.util.spec_from_file_location("prepare_build_identity", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
PREPARE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREPARE)


def _source_inventory(inputs: pathlib.Path) -> dict[str, object]:
    members = (
        ("linux_7.1.8-2.dsc", b"dsc"),
        ("linux_7.1.8.orig.tar.xz", b"orig"),
        ("linux_7.1.8-2.debian.tar.xz", b"debian"),
    )
    files = []
    for name, body in members:
        (inputs / name).write_bytes(body)
        files.append(
            {
                "name": name,
                "url": f"http://deb.invalid/{name}",
                "sha256": hashlib.sha256(body).hexdigest(),
                "size": len(body),
            }
        )
    return {"schema_version": 2, "source": "linux", "files": files}


def _write_lock(path: pathlib.Path, package: str, architecture: str) -> None:
    path.write_text(
        "package\tversion\tarchitecture\turi\tsize\tsha256\n"
        f"{package}\t1:2.3-4\t{architecture}\thttp://deb.invalid/package.deb\t1\t"
        f"{'a' * 64}\n",
        encoding="utf-8",
    )


def test_dpkg_multiarch_package_name_is_not_double_qualified(
    tmp_path: pathlib.Path,
) -> None:
    lock = tmp_path / "lock.tsv"
    _write_lock(lock, "libc6:amd64", "amd64")
    assert PREPARE.dependency_lock(lock) == {
        "libc6:amd64": f"1:2.3-4@{'a' * 64}"
    }


def test_mismatched_multiarch_qualifier_is_rejected(tmp_path: pathlib.Path) -> None:
    lock = tmp_path / "lock.tsv"
    _write_lock(lock, "libc6:arm64", "amd64")
    with pytest.raises(SystemExit, match="qualifier differs"):
        PREPARE.dependency_lock(lock)


def test_source_hashes_use_exact_inventory_names_for_a_new_upstream_version(
    tmp_path: pathlib.Path,
) -> None:
    inventory = _source_inventory(tmp_path)
    dsc, members = PREPARE.source_hashes(tmp_path, inventory)
    assert dsc == hashlib.sha256(b"dsc").hexdigest()
    assert members == {
        "linux_7.1.8.orig.tar.xz": hashlib.sha256(b"orig").hexdigest(),
        "linux_7.1.8-2.debian.tar.xz": hashlib.sha256(b"debian").hexdigest(),
    }


def test_source_hashes_reject_a_name_uri_disagreement(tmp_path: pathlib.Path) -> None:
    inventory = _source_inventory(tmp_path)
    files = inventory["files"]
    assert isinstance(files, list) and isinstance(files[0], dict)
    files[0]["url"] = "http://deb.invalid/wrong.dsc"
    with pytest.raises(SystemExit, match="differs from its URL"):
        PREPARE.source_hashes(tmp_path, inventory)


def test_resolved_kernel_config_parser_records_disabled_symbols(
    tmp_path: pathlib.Path,
) -> None:
    config = tmp_path / ".config"
    config.write_text(
        'CONFIG_LOCALVERSION="-v3-amd64"\n'
        "# CONFIG_MODULE_SIG is not set\n"
        "CONFIG_RUST=y\n",
        encoding="utf-8",
    )
    assert PREPARE.parse_kernel_config(config) == {
        "CONFIG_LOCALVERSION": '"-v3-amd64"',
        "CONFIG_MODULE_SIG": "n",
        "CONFIG_RUST": "y",
    }

    config.write_text("CONFIG_RUST=y\nCONFIG_RUST=n\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate Kconfig"):
        PREPARE.parse_kernel_config(config)


def test_publication_epoch_starts_the_next_utc_date() -> None:
    source_epoch = 1_786_077_877
    publication_epoch = PREPARE.next_utc_date_epoch(source_epoch)
    assert publication_epoch == 1_786_147_200
    assert publication_epoch > source_epoch
    assert publication_epoch % 86400 == 0


@pytest.mark.parametrize("mode", PREPARE.LTO_MODES)
def test_lto_policy_is_written_to_every_flavor_fragment(
    tmp_path: pathlib.Path, mode: str
) -> None:
    config = tmp_path / "debian" / "config" / "amd64"
    config.mkdir(parents=True)
    for flavor in PREPARE.FLAVORS:
        (config / f"config.{flavor}-amd64").write_text(
            f"CONFIG_DKC_X86_64_BASELINE_{flavor.upper()}=y\n", encoding="utf-8"
        )

    PREPARE.apply_lto_policy(tmp_path, mode)

    expected = set(PREPARE.LTO_CONFIG_LINES[mode])
    for flavor in PREPARE.FLAVORS:
        lines = set(
            (config / f"config.{flavor}-amd64")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert expected <= lines
        assert PREPARE.LTO_FRAGMENT_MARKER in lines


@pytest.mark.parametrize(
    "existing", ("CONFIG_LTO_NONE=y", "CONFIG_DEBUG_INFO_BTF=y")
)
def test_lto_policy_refuses_an_existing_choice(
    tmp_path: pathlib.Path, existing: str
) -> None:
    config = tmp_path / "debian" / "config" / "amd64"
    config.mkdir(parents=True)
    for flavor in PREPARE.FLAVORS:
        (config / f"config.{flavor}-amd64").write_text(
            f"{existing}\n", encoding="utf-8"
        )
    with pytest.raises(SystemExit, match="already carries"):
        PREPARE.apply_lto_policy(tmp_path, "thin")
