"""Fast tests for the bounded toolchain attestation helpers."""

from __future__ import annotations

import importlib.util
import lzma
import pathlib
import re
from types import ModuleType

import pytest


SCRIPT = (
    pathlib.Path(__file__).parent.parent
    / "scripts"
    / "in-container"
    / "attest-one-build.py"
)


def load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("attest_one_build", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ATTEST = load_script()


def test_savedcmd_is_the_current_kbuild_record_name(tmp_path: pathlib.Path) -> None:
    record = tmp_path / ".example.o.cmd"
    record.write_text(
        "savedcmd_drivers/example.o := clang-21 -c -o drivers/example.o drivers/example.c\n"
        "source_drivers/example.o := drivers/example.c\n",
        encoding="utf-8",
    )
    assert list(ATTEST.iter_saved_commands([record])) == [
        (
            record,
            "drivers/example.o",
            "clang-21 -c -o drivers/example.o drivers/example.c",
        )
    ]


def test_shell_tokenizer_does_not_regex_match_argument_substrings() -> None:
    tokens = ATTEST.shell_tokens(
        "clang-21 -Ddescription=cc -c a.c && ld.lld-21 -r a.o -o a.ko",
        "a.ko",
    )
    assert "clang-21" in tokens
    assert "ld.lld-21" in tokens
    assert "cc" not in tokens


def test_wrong_or_gnu_tool_is_rejected_before_path_resolution() -> None:
    with pytest.raises(SystemExit, match="unexpected LLVM tool major"):
        ATTEST.resolve_llvm_tool("clang-20", 21)
    with pytest.raises(SystemExit, match="unexpected GNU or unversioned tool"):
        ATTEST.resolve_llvm_tool("x86_64-linux-gnu-gcc-14", 21)
    for tool in ("clang", "clang++", "ld.lld", "llvm-ar", "llvm-objcopy"):
        with pytest.raises(SystemExit, match="unversioned LLVM tool"):
            ATTEST.resolve_llvm_tool(tool, 21)


def test_config_parser_rejects_duplicate_symbols(tmp_path: pathlib.Path) -> None:
    config = tmp_path / ".config"
    config.write_text("CONFIG_RUST=y\nCONFIG_RUST=n\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate Kconfig"):
        ATTEST.parse_config(config)


def test_private_key_scan_does_not_confuse_public_certificate_or_metadata() -> None:
    assert ATTEST.is_possible_private_key(pathlib.Path("certs/signing_key.pem"))
    assert ATTEST.is_possible_private_key(pathlib.Path("release-signing.p12"))
    assert not ATTEST.is_possible_private_key(pathlib.Path("certs/signing_key.x509"))
    assert not ATTEST.is_possible_private_key(pathlib.Path("certs/.signing_key.x509.cmd"))


def test_btf_policy_checks_both_presence_and_absence(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elf = tmp_path / "vmlinux"
    elf.touch()
    sections = "  [12] .BTF PROGBITS\n"
    monkeypatch.setattr(ATTEST, "readelf", lambda *_args: sections)
    ATTEST.require_btf("llvm-readelf-21", elf, "vmlinux")
    with pytest.raises(SystemExit, match="unexpectedly has a .BTF section"):
        ATTEST.forbid_btf("llvm-readelf-21", elf, "vmlinux")

    monkeypatch.setattr(ATTEST, "readelf", lambda *_args: "  [12] .text PROGBITS\n")
    ATTEST.forbid_btf("llvm-readelf-21", elf, "vmlinux")
    with pytest.raises(SystemExit, match="has no .BTF section"):
        ATTEST.require_btf("llvm-readelf-21", elf, "vmlinux")


def test_module_sample_is_deterministic_and_covers_subsystems(tmp_path: pathlib.Path) -> None:
    modules: dict[str, pathlib.Path] = {}
    for index, subsystem in enumerate(("arch", "crypto", "drivers", "fs", "net", "sound")):
        for member in range(6):
            identity = f"{subsystem}/example-{member}.ko"
            path = tmp_path / f"{index}-{member}.ko"
            path.write_bytes(b"x" * (index * 10 + member + 1))
            modules[identity] = path

    first = ATTEST.select_module_sample(modules, limit=20)
    second = ATTEST.select_module_sample(modules, limit=20)
    assert first == second
    assert len(first) == 20
    assert {identity.partition("/")[0] for identity in first} == {
        "arch",
        "crypto",
        "drivers",
        "fs",
        "net",
        "sound",
    }


def test_target_normalization_accepts_only_build_relative_paths(tmp_path: pathlib.Path) -> None:
    build = tmp_path / "build"
    assert ATTEST.normalize_target(f"{build}/drivers/a.ko", build) == "drivers/a.ko"
    assert ATTEST.normalize_target("./drivers/a.ko", build) == "drivers/a.ko"
    assert (
        ATTEST.normalize_target("arch/x86/boot/compressed/../voffset.h", build)
        == "arch/x86/boot/voffset.h"
    )
    with pytest.raises(SystemExit, match="unsafe Kbuild command target"):
        ATTEST.normalize_target("../outside.o", build)


def test_captured_kbuild_inventory_replays_without_command_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = tmp_path / "kbuild-commands.tsv.xz"
    content = (
        "drivers/a.ko\tdrivers/.a.ko.cmd\t"
        "clang-21 -c drivers/a.c && ld.lld-21 -r drivers/a.o -o drivers/a.ko\n"
    )
    with lzma.open(inventory, "wt", encoding="utf-8") as stream:
        stream.write(content)

    def fake_resolve(token: str, _llvm_major: int) -> str | None:
        name = pathlib.PurePath(token).name
        return name if name in {"clang-21", "ld.lld-21"} else None

    monkeypatch.setattr(ATTEST, "resolve_llvm_tool", fake_resolve)
    records, count, tools, normalized = ATTEST.load_kbuild_inventory(inventory, 21)
    assert count == 1
    assert normalized == content
    assert tools == {"clang-21": 1, "ld.lld-21": 1}
    assert records["drivers/a.ko"][0][0].as_posix() == "drivers/.a.ko.cmd"

    with lzma.open(inventory, "wt", encoding="utf-8") as stream:
        stream.write(content.replace("drivers/.a.ko.cmd", "../outside.cmd"))
    with pytest.raises(SystemExit, match="unsafe captured Kbuild record"):
        ATTEST.load_kbuild_inventory(inventory, 21)


def test_header_symlink_must_resolve_to_the_exact_conventional_path(
    tmp_path: pathlib.Path,
) -> None:
    relative = "usr/lib/modules/release/build"
    link = tmp_path / relative
    link.parent.mkdir(parents=True)
    link.symlink_to("../../../src/linux-headers-release")
    ATTEST.require_relative_symlink(
        tmp_path, relative, "usr/src/linux-headers-release"
    )

    link.unlink()
    link.symlink_to("../../../src/dkc-linux-headers-release")
    with pytest.raises(SystemExit, match="resolves incorrectly"):
        ATTEST.require_relative_symlink(
            tmp_path, relative, "usr/src/linux-headers-release"
        )


def test_checksum_inventory_requires_safe_unique_exact_records(
    tmp_path: pathlib.Path,
) -> None:
    artifact = tmp_path / "one.deb"
    artifact.write_bytes(b"one")
    digest = ATTEST.sha256(artifact)
    records = ATTEST.checksum_records(f"{digest} 3 one.deb", ".buildinfo")
    ATTEST.require_checksum_set(records, [artifact], ".buildinfo")

    with pytest.raises(SystemExit, match="unsafe or duplicate"):
        ATTEST.checksum_records(
            f"{digest} 3 ../one.deb\n{digest} 3 ../one.deb", ".changes"
        )
    with pytest.raises(SystemExit, match="file set differs"):
        ATTEST.require_checksum_set(records, [], ".buildinfo")


def test_deb822_parser_rejects_duplicate_and_malformed_fields(
    tmp_path: pathlib.Path,
) -> None:
    record = tmp_path / "record.buildinfo"
    record.write_text("Source: dkc-linux\nVersion: 1\n", encoding="utf-8")
    assert ATTEST.parse_deb822(record) == {"Source": "dkc-linux", "Version": "1"}

    record.write_text("Source: one\nSource: two\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="duplicate Deb822"):
        ATTEST.parse_deb822(record)

    record.write_text("Source: one\nnot-a-field\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="malformed or duplicate Deb822"):
        ATTEST.parse_deb822(record)


def test_binary_inventory_rejects_malformed_names() -> None:
    assert ATTEST.binary_names("dkc-linux-a, dkc-linux-b\n") == {
        "dkc-linux-a",
        "dkc-linux-b",
    }
    with pytest.raises(SystemExit, match="malformed binary"):
        ATTEST.binary_names("dkc-linux-a /tmp/escape")


def test_attestation_policy_digest_uses_shared_identity_normalization() -> None:
    config = {
        "CONFIG_LOCALVERSION": '"-v4-amd64"',
        "CONFIG_BUILD_SALT": '"one"',
        "CONFIG_RUST": "y",
    }
    changed_identity = {**config, "CONFIG_BUILD_SALT": '"two"'}
    assert ATTEST.policy_config_digest(config) == ATTEST.policy_config_digest(
        changed_identity
    )


def test_debian_metadata_timestamps_require_rfc_date_with_timezone() -> None:
    expected = 1_786_147_200
    for label in ("Build-Date", "Date"):
        assert ATTEST.timestamp_epoch("Sat, 08 Aug 2026 00:00:00 +0000", label) == expected
        with pytest.raises(SystemExit, match="lacks a timezone"):
            ATTEST.timestamp_epoch("Sat, 08 Aug 2026 00:00:00", label)


def test_every_flavor_build_accounts_for_arch_independent_packages() -> None:
    # Parallel flavor jobs are self-contained. Each uses the complete binary
    # scope and inventories both amd64 and Architecture:all output.
    run = (SCRIPT.parent / "run-one-build.sh").read_text(encoding="utf-8")
    assert "build_scope=binary" in run
    assert "dh_listpackages_args=()" in run
    assert "pkg.dkc.nokbuild" not in run
    assert 'selected_packages="$(dh_listpackages "${dh_listpackages_args[@]}")"' in run


def test_build_disables_implicit_debug_symbol_packages_source_wide() -> None:
    run = (SCRIPT.parent / "run-one-build.sh").read_text(encoding="utf-8")
    assert 'DEB_BUILD_OPTIONS="parallel=${jobs} noautodbgsym"' in run


def test_kbuild_template_drops_the_current_stale_python_substitution() -> None:
    generator = (SCRIPT.parent / "generate-overlay-patches.py").read_text(
        encoding="utf-8"
    )
    assert 'Depends: ${shlibs:Depends}, ${misc:Depends}, ${python3:Depends}, pahole' in generator
    assert 'Depends: ${shlibs:Depends}, ${misc:Depends}, pahole' in generator


def test_header_tool_assignments_require_every_versioned_llvm_tool(
    tmp_path: pathlib.Path,
) -> None:
    variables = tmp_path / ".kernelvariables"
    variables.write_text(
        "LLVM = -21\n"
        "LLVM_PREFIX = \n"
        "LLVM_SUFFIX = -21\n"
        "CC = $(if $(DEBIAN_KERNEL_USE_CCACHE),$(CCACHE)) clang-21\n"
        "HOSTCC = clang-21\nHOSTCXX = clang++-21\nLD = ld.lld-21\n"
        "AR = llvm-ar-21\nNM = llvm-nm-21\nOBJCOPY = llvm-objcopy-21\n"
        "OBJDUMP = llvm-objdump-21\nREADELF = llvm-readelf-21\n"
        "STRIP = llvm-strip-21\nLLVM_LINK = llvm-link-21\n",
        encoding="utf-8",
    )
    ATTEST.validate_kernelvariables(variables, 21)
    variables.write_text(variables.read_text().replace("LD = ld.lld-21", "LD = ld"))
    with pytest.raises(SystemExit, match="header tool assignment LD"):
        ATTEST.validate_kernelvariables(variables, 21)
