"""Focused fixtures for baseline/no-SIMD Kbuild command classification."""

from __future__ import annotations

import importlib.util
import pathlib
from dataclasses import replace

import pytest

from dkc.flavors import load_flavor_policy

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "in-container" / "audit-kbuild-commands.py"
spec = importlib.util.spec_from_file_location("audit_kbuild_commands", SCRIPT)
assert spec and spec.loader
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


def _audit(
    tmp_path: pathlib.Path,
    commands: list[tuple[str, str]],
    lto_mode: str = "none",
    lto_exclusions: frozenset[str] = frozenset(),
) -> dict[str, object]:
    path = tmp_path / "commands.tsv"
    path.write_text(
        "".join(
            f"{target}\t.{target}.cmd\t{command}\n"
            for target, command in commands
        )
    )
    policy = replace(
        load_flavor_policy(ROOT / "config" / "flavors" / "v3.toml"),
        intentional_fpu_objects=(),
    )
    old = audit_module.MINIMUM_COUNTS
    old_host = audit_module.MINIMUM_HOST_C
    old_exclusions = audit_module.LTO_EXCLUDED_C_TARGETS
    audit_module.MINIMUM_COUNTS = {name: 0 for name in old}
    audit_module.MINIMUM_HOST_C = {name: 0 for name in old_host}
    audit_module.LTO_EXCLUDED_C_TARGETS = lto_exclusions
    try:
        return audit_module.audit(path, policy, lto_mode)
    finally:
        audit_module.MINIMUM_COUNTS = old
        audit_module.MINIMUM_HOST_C = old_host
        audit_module.LTO_EXCLUDED_C_TARGETS = old_exclusions


def _normal(*extra: str) -> str:
    flags = " ".join(extra)
    return (
        "clang-21 -D__KERNEL__ -march=x86-64-v3 "
        "-mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx -mno-sse4a "
        f"{flags} -c -o kernel/foo.o /work/build/linux/kernel/foo.c"
    )


def test_normal_c_requires_final_no_simd_flags_after_baseline(tmp_path: pathlib.Path) -> None:
    report = _audit(tmp_path, [("kernel/foo.o", _normal())])
    assert report["status"] == "PASS"


@pytest.mark.parametrize(
    ("mode", "flags"),
    (
        ("thin", ("-fno-lto", "-flto=thin", "-fsplit-lto-unit", "-fvisibility=hidden")),
        ("full", ("-fno-lto", "-flto", "-fvisibility=hidden")),
    ),
)
def test_lto_mode_requires_the_exact_compile_flag(
    tmp_path: pathlib.Path, mode: str, flags: tuple[str, ...]
) -> None:
    report = _audit(tmp_path, [("kernel/foo.o", _normal(*flags))], mode)
    assert report["status"] == "PASS"
    assert report["lto_mode"] == mode
    assert report["counts"]["lto_c"] == 1


def test_lto_mode_rejects_a_missing_or_wrong_flag(tmp_path: pathlib.Path) -> None:
    missing = _audit(tmp_path, [("kernel/foo.o", _normal())], "thin")
    wrong = _audit(tmp_path, [("kernel/foo.o", _normal("-flto"))], "thin")
    assert missing["status"] == "FAIL"
    assert wrong["status"] == "FAIL"
    assert {item["kind"] for item in missing["errors"]} == {"c-lto"}
    assert {item["kind"] for item in wrong["errors"]} == {"c-lto"}


def test_reviewed_lto_exclusion_requires_explicit_fno_lto(
    tmp_path: pathlib.Path,
) -> None:
    target = "arch/x86/power/cpu.o"
    exclusions = frozenset({target})
    passing = _audit(
        tmp_path,
        [(target, _normal("-fno-lto"))],
        "thin",
        exclusions,
    )
    assert passing["status"] == "PASS"
    assert passing["lto_excluded_c_seen"] == 1

    missing_disable = _audit(tmp_path, [(target, _normal())], "thin", exclusions)
    assert missing_disable["status"] == "FAIL"
    assert {item["kind"] for item in missing_disable["errors"]} == {
        "c-lto-exclusion"
    }


def test_lto_exclusion_inventory_must_be_exact(tmp_path: pathlib.Path) -> None:
    target = "arch/x86/power/cpu.o"
    report = _audit(tmp_path, [("kernel/foo.o", _normal("-fno-lto", "-flto=thin", "-fsplit-lto-unit", "-fvisibility=hidden"))], "thin", frozenset({target}))
    assert report["status"] == "FAIL"
    assert {item["kind"] for item in report["errors"]} == {"stale-lto-exclusion"}


def test_baseline_after_the_only_disable_set_is_rejected(tmp_path: pathlib.Path) -> None:
    command = (
        "clang-21 -D__KERNEL__ -mno-sse -mno-mmx -mno-sse2 -mno-3dnow "
        "-mno-avx -mno-sse4a -march=x86-64-v3 "
        "-c -o kernel/foo.o /work/build/linux/kernel/foo.c"
    )
    report = _audit(tmp_path, [("kernel/foo.o", command)])
    assert report["status"] == "FAIL"
    assert {error["kind"] for error in report["errors"]} == {"c-order"}


def test_boot_and_host_tools_must_not_receive_flavor_march(tmp_path: pathlib.Path) -> None:
    boot = (
        "clang-21 -D__KERNEL__ -march=x86-64-v3 -mno-mmx -mno-sse "
        "-c -o arch/x86/boot/foo.o /src/arch/x86/boot/foo.c"
    )
    host = "clang-21 -march=x86-64-v3 -c -o scripts/foo.o /src/scripts/foo.c"
    report = _audit(
        tmp_path,
        [("arch/x86/boot/foo.o", boot), ("scripts/foo.o", host)],
    )
    assert report["status"] == "FAIL"
    assert {error["kind"] for error in report["errors"]} == {"host-c", "special-baseline"}


def test_startup_and_realmode_wrapper_are_normal_64_bit_targets(tmp_path: pathlib.Path) -> None:
    report = _audit(
        tmp_path,
        [
            ("arch/x86/boot/startup/map_kernel.o", _normal()),
            ("arch/x86/realmode/init.o", _normal()),
        ],
    )
    assert report["status"] == "PASS"


def test_special_assembly_must_not_receive_the_flavor_baseline(
    tmp_path: pathlib.Path,
) -> None:
    command = (
        "clang-21 -D__KERNEL__ -march=x86-64-v3 -c "
        "-o arch/x86/boot/setup.o /src/arch/x86/boot/setup.S"
    )
    report = _audit(tmp_path, [("arch/x86/boot/setup.o", command)])
    assert report["status"] == "FAIL"
    assert {error["kind"] for error in report["errors"]} == {
        "special-assembly-baseline"
    }


def test_positive_simd_needs_marker_and_exact_object_allowlist(tmp_path: pathlib.Path) -> None:
    report = _audit(tmp_path, [("kernel/foo.o", _normal("-msse", "-msse2"))])
    kinds = {error["kind"] for error in report["errors"]}
    assert {"fpu-marker", "fpu-allowlist"} <= kinds


def test_kernel_rust_requires_matching_cpu_and_final_feature_disable(tmp_path: pathlib.Path) -> None:
    rust = (
        "rustc --target=./scripts/target.json -Ctarget-cpu=x86-64-v3 "
        "-Ctarget-feature=-sse,-sse2,-sse3,-ssse3,-sse4.1,-sse4.2,-avx,-avx2 "
        "--emit=obj=rust/foo.o /src/rust/foo.rs"
    )
    report = _audit(tmp_path, [("rust/foo.o", rust)])
    assert report["status"] == "PASS"


def test_kernel_rust_rejects_late_vector_feature_reenable(tmp_path: pathlib.Path) -> None:
    rust = (
        "rustc --target=./scripts/target.json -Ctarget-cpu=x86-64-v3 "
        "-Ctarget-feature=-sse,-sse2,-sse3,-ssse3,-sse4.1,-sse4.2,-avx,-avx2 "
        "-Ctarget-feature=+avx512f,+aes --emit=obj=rust/foo.o /src/rust/foo.rs"
    )
    report = _audit(tmp_path, [("rust/foo.o", rust)])
    assert report["status"] == "FAIL"
    assert "rust-vector-reenable" in {error["kind"] for error in report["errors"]}


def test_kernel_rust_rejects_split_late_vector_feature_reenable(
    tmp_path: pathlib.Path,
) -> None:
    rust = (
        "rustc --target ./scripts/target.json -C target-cpu=x86-64-v3 "
        "-C target-feature=-sse,-sse2,-sse3,-ssse3,-sse4.1,-sse4.2,-avx,-avx2 "
        "-C target-feature=+avx512f --emit=obj=rust/foo.o /src/rust/foo.rs"
    )
    report = _audit(tmp_path, [("rust/foo.o", rust)])
    assert report["status"] == "FAIL"
    assert "rust-vector-reenable" in {error["kind"] for error in report["errors"]}


def test_less_common_positive_vector_flags_are_rejected(tmp_path: pathlib.Path) -> None:
    report = _audit(tmp_path, [("kernel/foo.o", _normal("-mfma", "-mamx-tile"))])
    kinds = {error["kind"] for error in report["errors"]}
    assert {"fpu-marker", "fpu-allowlist"} <= kinds


def test_special_c_rejects_positive_simd_even_without_flavor_march(
    tmp_path: pathlib.Path,
) -> None:
    special = (
        "clang-21 -D__KERNEL__ -mno-mmx -mno-sse -msse2 -c "
        "-o arch/x86/boot/setup.o /src/arch/x86/boot/setup.c"
    )
    report = _audit(tmp_path, [("arch/x86/boot/setup.o", special)])
    assert report["status"] == "FAIL"
    assert "special-simd" in {error["kind"] for error in report["errors"]}
