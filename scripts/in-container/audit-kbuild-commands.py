#!/usr/bin/env python3
"""Audit every captured Kbuild compile command for one DKC flavor.

This is deliberately a command-policy audit, not the final machine-code audit.
It proves where the baseline and no-SIMD controls reached Kbuild, classifies
targets that replace the architecture flags, and binds every explicit C FPU
exception to an exact source-versioned object allowlist.
"""

from __future__ import annotations

import json
import lzma
import pathlib
import re
import shlex
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dkc.flavors import FlavorPolicy, load_flavor_policy  # noqa: E402

MINIMUM_COUNTS = {
    "records": 30_000,
    "normal_c": 19_000,
    "special_c": 50,
    "kernel_rust": 10,
}

# Enabling kernel LTO disables BTF for this source/toolchain combination.  That
# removes the 37 host-C records used to build tools/bpf/resolve_btfids, so the
# host-tool coverage floor must describe the two deliberately different build
# graphs instead of assuming that BTF is always enabled.
MINIMUM_HOST_C = {"none": 50, "thin": 40, "full": 40}

# Linux 7.1.7 deliberately removes CC_FLAGS_LTO from these normal 64-bit C
# objects.  They execute before ordinary kernel relocation, are linked as
# userspace vDSOs or standalone purgatory code, avoid an LLVM suspend/resume
# inlining bug, or probe the module ELF format.  Keep this exact and require
# both absence of a positive LTO flag and presence of -fno-lto: an upstream
# addition or deletion must trigger review rather than silently widening the
# exception.
LTO_EXCLUDED_C_TARGETS = frozenset(
    {
        "arch/x86/boot/startup/gdt_idt.o",
        "arch/x86/boot/startup/map_kernel.o",
        "arch/x86/boot/startup/sev-startup.o",
        "arch/x86/boot/startup/sme.o",
        "arch/x86/entry/vdso/vdso32/vclock_gettime.o",
        "arch/x86/entry/vdso/vdso32/vgetcpu.o",
        "arch/x86/entry/vdso/vdso64/vclock_gettime.o",
        "arch/x86/entry/vdso/vdso64/vgetcpu.o",
        "arch/x86/entry/vdso/vdso64/vgetrandom.o",
        "arch/x86/power/cpu.o",
        "arch/x86/purgatory/purgatory.o",
        "arch/x86/purgatory/sha256.o",
        "arch/x86/purgatory/string.o",
        "scripts/mod/empty.o",
    }
)


def _is_special_c(target: str) -> bool:
    """Targets that replace, rather than refine, normal x86 KBUILD_CFLAGS.

    `boot/startup` and `realmode/init.o` are deliberate exceptions to their
    parent directories: both retain the ordinary 64-bit architecture flags.
    The narrower descendants below are the rules that really switch to the
    16-bit setup/decompressor or EFI-stub flag sets.
    """
    return (
        (target.startswith("arch/x86/boot/") and not target.startswith("arch/x86/boot/startup/"))
        or target.startswith("arch/x86/realmode/rm/")
        or target.startswith("drivers/firmware/efi/libstub/")
    )


def _open(path: pathlib.Path):
    if path.suffix == ".xz":
        return lzma.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def _source(tokens: list[str], suffix: str) -> str | None:
    for token in reversed(tokens):
        if token.endswith(suffix):
            return token
    return None


def _is_clang_compile(tokens: list[str]) -> bool:
    return "-c" in tokens and any(
        re.fullmatch(r"clang(?:-[0-9]+)?", pathlib.PurePosixPath(token).name)
        for token in tokens
    )


def _is_kernel_rust(tokens: list[str]) -> bool:
    for index, token in enumerate(tokens):
        if token.startswith("--target="):
            target = token.removeprefix("--target=")
        elif token == "--target" and index + 1 < len(tokens):
            target = tokens[index + 1]
        else:
            continue
        if target == "./scripts/target.json" or target.endswith("/scripts/target.json"):
            return True
    return False


def _positive_simd_flags(tokens: list[str]) -> list[str]:
    vector_flag = re.compile(
        r"-m(?:sse.*|avx.*|mmx|3dnow.*|fma.*|f16c|amx.*|sha|aes|pclmul|gfni|vaes|vpclmulqdq)"
    )
    return sorted(
        {
            token
            for token in tokens
            if vector_flag.fullmatch(token) and not token.startswith("-mno-")
        }
    )


def _flag_positions(tokens: list[str], flag: str) -> list[int]:
    return [index for index, token in enumerate(tokens) if token == flag]


def _rust_codegen_options(tokens: list[str], name: str) -> list[tuple[str, int]]:
    prefix = f"-C{name}="
    split_prefix = f"{name}="
    options: list[tuple[str, int]] = []
    for index, token in enumerate(tokens):
        if token.startswith(prefix):
            options.append((token.removeprefix(prefix), index))
        elif token == "-C" and index + 1 < len(tokens) and tokens[index + 1].startswith(split_prefix):
            options.append((tokens[index + 1].removeprefix(split_prefix), index + 1))
    return options


def _rust_feature_states(options: list[tuple[str, int]]) -> dict[str, tuple[str, int]]:
    states: dict[str, tuple[str, int]] = {}
    for value, index in options:
        for item in value.split(","):
            if not re.fullmatch(r"[+-][A-Za-z0-9_.-]+", item):
                continue
            states[item[1:]] = (item[0], index)
    return states


def audit(
    path: pathlib.Path, policy: FlavorPolicy, lto_mode: str = "none"
) -> dict[str, object]:
    expected_lto_flags = {
        "none": [],
        "thin": ["-flto=thin"],
        "full": ["-flto"],
    }
    if lto_mode not in expected_lto_flags:
        raise ValueError(f"unknown LTO mode {lto_mode!r}")
    counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    seen_fpu: set[str] = set()
    seen_lto_exclusions: set[str] = set()

    def reject(target: str, kind: str, detail: str) -> None:
        if len(errors) < 200:
            errors.append({"target": target, "kind": kind, "detail": detail})

    with _open(path) as stream:
        for number, line in enumerate(stream, 1):
            counts["records"] += 1
            fields = line.rstrip("\n").split("\t", 2)
            if len(fields) != 3:
                reject(f"line:{number}", "malformed", "record does not have three TSV fields")
                continue
            target, _command_file, command = fields
            try:
                tokens = shlex.split(command)
            except ValueError as exc:
                reject(target, "shell-parse", str(exc))
                continue

            if any(pathlib.PurePosixPath(token).name == "rustc" for token in tokens):
                if _is_kernel_rust(tokens):
                    counts["kernel_rust"] += 1
                    cpu_options = _rust_codegen_options(tokens, "target-cpu")
                    feature_options = _rust_codegen_options(tokens, "target-feature")
                    expected_features = policy.rust_target_feature_flag.removeprefix(
                        "-Ctarget-feature="
                    )
                    cpu_positions = [
                        position
                        for value, position in cpu_options
                        if value == policy.rust_target_cpu
                    ]
                    feature_positions = [
                        position
                        for value, position in feature_options
                        if value == expected_features
                    ]
                    conflicting = sorted(
                        value
                        for value, _position in cpu_options
                        if value != policy.rust_target_cpu
                    )
                    if len(cpu_positions) != 1 or conflicting:
                        reject(target, "rust-baseline", f"cpu={cpu_positions}, conflicting={conflicting}")
                    if not feature_positions:
                        reject(target, "rust-no-simd", "reviewed target-feature disablement is absent")
                    elif cpu_positions and feature_positions[-1] < cpu_positions[-1]:
                        reject(target, "rust-order", "no-SIMD target features are not final after target CPU")
                    feature_states = _rust_feature_states(feature_options)
                    for required in policy.rust_no_simd_features:
                        state = feature_states.get(required)
                        if state is None or state[0] != "-":
                            reject(target, "rust-effective-no-simd", f"final {required} state is {state!r}")
                        elif cpu_positions and state[1] < cpu_positions[-1]:
                            reject(target, "rust-effective-order", f"final -{required} occurs before target CPU")
                    enabled_vector = sorted(
                        name
                        for name, (state, _position) in feature_states.items()
                        if state == "+"
                        and re.fullmatch(
                            r"(?:sse.*|avx.*|fma.*|f16c|mmx|x87|amx.*|"
                            r"sha|aes|pclmulqdq|gfni|vaes|vpclmulqdq)",
                            name,
                        )
                    )
                    if enabled_vector:
                        reject(target, "rust-vector-reenable", f"final enabled features: {enabled_vector}")
                else:
                    counts["host_rust"] += 1
                    if any(
                        value.startswith("x86-64-v")
                        for value, _position in _rust_codegen_options(tokens, "target-cpu")
                    ):
                        reject(target, "host-rust", "flavor baseline leaked into a host Rust tool")

            if not _is_clang_compile(tokens):
                continue
            marches = [token for token in tokens if token.startswith("-march=")]
            flavor_marches = [token for token in marches if token.startswith("-march=x86-64-v")]
            source = _source(tokens, ".c")
            if source is None:
                if _source(tokens, ".S") is not None:
                    counts["assembly"] += 1
                    if _is_special_c(target) and flavor_marches:
                        reject(
                            target,
                            "special-assembly-baseline",
                            f"special assembly target received {flavor_marches}",
                        )
                    elif "-D__KERNEL__" not in tokens and flavor_marches:
                        reject(
                            target,
                            "host-assembly",
                            f"flavor baseline leaked into host assembly: {flavor_marches}",
                        )
                continue
            counts["c_compile"] += 1

            if "-D__KERNEL__" not in tokens:
                counts["host_c"] += 1
                if flavor_marches:
                    reject(target, "host-c", f"flavor baseline leaked into host compilation: {flavor_marches}")
                continue

            if _is_special_c(target):
                counts["special_c"] += 1
                if flavor_marches:
                    reject(target, "special-baseline", f"special target received {flavor_marches}")
                for required in ("-mno-mmx", "-mno-sse"):
                    if required not in tokens:
                        reject(target, "special-no-simd", f"missing {required}")
                positive = _positive_simd_flags(tokens)
                if positive:
                    reject(target, "special-simd", f"special target enables SIMD: {positive}")
                continue

            counts["normal_c"] += 1
            expected_march = f"-march={policy.compiler_march}"
            if marches != [expected_march]:
                reject(target, "c-baseline", f"march flags are {marches!r}, expected {[expected_march]!r}")
                continue
            lto_flags = [token for token in tokens if token == "-flto" or token.startswith("-flto=")]
            lto_excluded = lto_mode != "none" and target in LTO_EXCLUDED_C_TARGETS
            expected_target_lto = [] if lto_excluded else expected_lto_flags[lto_mode]
            if lto_flags != expected_target_lto:
                reject(
                    target,
                    "c-lto",
                    f"LTO flags are {lto_flags!r}, expected {expected_target_lto!r}",
                )
            elif lto_excluded:
                seen_lto_exclusions.add(target)
                counts["lto_excluded_c"] += 1
                if tokens.count("-fno-lto") != 1:
                    reject(target, "c-lto-exclusion", "reviewed target lacks exactly one -fno-lto")
            elif lto_flags:
                counts["lto_c"] += 1
                if tokens.count("-fno-lto") != 1 or tokens.index("-fno-lto") > tokens.index(lto_flags[0]):
                    reject(target, "c-lto-order", "positive LTO is not final after exactly one -fno-lto")
                expected_companions = (
                    {"-fsplit-lto-unit", "-fvisibility=hidden"}
                    if lto_mode == "thin"
                    else {"-fvisibility=hidden"}
                )
                missing_companions = sorted(expected_companions - set(tokens))
                if missing_companions:
                    reject(target, "c-lto-companion", f"missing {missing_companions}")
                if lto_mode == "full" and "-fsplit-lto-unit" in tokens:
                    reject(target, "c-lto-companion", "full LTO unexpectedly uses -fsplit-lto-unit")
            march_index = tokens.index(expected_march)
            for required in policy.c_no_simd_flags:
                positions = _flag_positions(tokens, required)
                if not positions:
                    reject(target, "c-no-simd", f"missing {required}")
                elif positions[-1] < march_index:
                    reject(target, "c-order", f"final {required} occurs before {expected_march}")

            positive = _positive_simd_flags(tokens)
            fpu_marker = "-D_LINUX_FPU_COMPILATION_UNIT" in tokens
            if positive or fpu_marker:
                counts["intentional_fpu_c"] += 1
                seen_fpu.add(target)
                if not fpu_marker:
                    reject(target, "fpu-marker", f"positive SIMD flags lack kernel marker: {positive}")
                if not positive:
                    reject(target, "fpu-flags", "kernel FPU marker lacks explicit positive SIMD flags")
                if target not in policy.intentional_fpu_objects:
                    reject(target, "fpu-allowlist", "object is not in the exact reviewed allowlist")

    stale = sorted(set(policy.intentional_fpu_objects) - seen_fpu)
    for target in stale:
        reject(target, "stale-fpu-allowlist", "allowlisted object was not compiled with CC_FLAGS_FPU")
    if lto_mode != "none":
        for target in sorted(LTO_EXCLUDED_C_TARGETS - seen_lto_exclusions):
            reject(target, "stale-lto-exclusion", "reviewed LTO exclusion was not observed")
    minimum_counts = {**MINIMUM_COUNTS, "host_c": MINIMUM_HOST_C[lto_mode]}
    for name, minimum in minimum_counts.items():
        if counts[name] < minimum:
            reject("inventory", "coverage", f"{name}={counts[name]} is below {minimum}")

    return {
        "schema_version": 1,
        "status": "PASS" if not errors else "FAIL",
        "flavor": policy.flavor,
        "compiler_march": policy.compiler_march,
        "lto_mode": lto_mode,
        "lto_excluded_c_expected": len(LTO_EXCLUDED_C_TARGETS) if lto_mode != "none" else 0,
        "lto_excluded_c_seen": len(seen_lto_exclusions),
        "counts": dict(sorted(counts.items())),
        "intentional_fpu_objects_expected": len(policy.intentional_fpu_objects),
        "intentional_fpu_objects_seen": len(seen_fpu),
        "errors": errors,
        "errors_truncated": len(errors) == 200,
    }


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: audit-kbuild-commands.py <commands.tsv[.xz]> "
            "<policy.toml> <report.json> <none|thin|full>",
            file=sys.stderr,
        )
        return 2
    commands, policy_path, report_path = map(pathlib.Path, sys.argv[1:4])
    lto_mode = sys.argv[4]
    policy = load_flavor_policy(policy_path)
    try:
        report = audit(commands, policy, lto_mode)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report["status"] != "PASS":
        print(f"Kbuild command audit FAIL: {len(report['errors'])} recorded violations", file=sys.stderr)
        return 1
    print(
        "Kbuild command audit PASS: "
        f"{report['counts']['normal_c']} normal C, "
        f"{report['counts']['kernel_rust']} kernel Rust, "
        f"{report['intentional_fpu_objects_seen']} reviewed FPU C objects",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
