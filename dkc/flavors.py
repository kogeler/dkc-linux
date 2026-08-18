"""Validated x86-64 flavor policy shared by build and audit code."""

from __future__ import annotations

import pathlib
import re
import tomllib
from dataclasses import dataclass

from .naming import FLAVORS

__all__ = [
    "C_NO_SIMD_FLAGS",
    "RUST_NO_SIMD_FEATURES",
    "FlavorPolicy",
    "FlavorPolicyError",
    "load_all_flavor_policies",
    "load_flavor_policy",
]

C_NO_SIMD_FLAGS: tuple[str, ...] = (
    "-mno-sse",
    "-mno-mmx",
    "-mno-sse2",
    "-mno-3dnow",
    "-mno-avx",
    "-mno-sse4a",
)
RUST_NO_SIMD_FEATURES: tuple[str, ...] = (
    "sse",
    "sse2",
    "sse3",
    "ssse3",
    "sse4.1",
    "sse4.2",
    "avx",
    "avx2",
)

_EXPECTED = {
    "v2": ("x86-64-v2", "CONFIG_DKC_X86_64_BASELINE_V2"),
    "v3": ("x86-64-v3", "CONFIG_DKC_X86_64_BASELINE_V3"),
    "v4": ("x86-64-v4", "CONFIG_DKC_X86_64_BASELINE_V4"),
}
_CPU_FLAG_RE = re.compile(r"^[a-z0-9_]+$")
_OBJECT_RE = re.compile(r"^[A-Za-z0-9_+./-]+\.o$")


class FlavorPolicyError(ValueError):
    """A flavor policy is incomplete, ambiguous, or unsafe."""


@dataclass(frozen=True)
class FlavorPolicy:
    schema_version: int
    flavor: str
    compiler_march: str
    rust_target_cpu: str
    kconfig_symbol: str
    cpu_flags: tuple[str, ...]
    c_no_simd_flags: tuple[str, ...]
    rust_no_simd_features: tuple[str, ...]
    intentional_fpu_artifact: str
    intentional_fpu_objects: tuple[str, ...]

    @property
    def rust_target_feature_flag(self) -> str:
        return "-Ctarget-feature=" + ",".join(
            f"-{feature}" for feature in self.rust_no_simd_features
        )


def _strings(data: object, field: str) -> tuple[str, ...]:
    if not isinstance(data, list) or not data or not all(isinstance(v, str) for v in data):
        raise FlavorPolicyError(f"{field} must be a non-empty string array")
    values = tuple(data)
    if len(values) != len(set(values)):
        raise FlavorPolicyError(f"{field} contains duplicates")
    return values


def load_flavor_policy(path: pathlib.Path) -> FlavorPolicy:
    """Load one policy and bind its external allowlist without globs."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlavorPolicyError(f"cannot read {path}: {exc}") from exc

    required = {
        "schema_version",
        "flavor",
        "compiler_march",
        "rust_target_cpu",
        "kconfig_symbol",
        "cpu_flags",
        "c_no_simd_flags",
        "rust_no_simd_features",
        "intentional_fpu_allowlist",
    }
    if set(raw) != required:
        raise FlavorPolicyError(
            f"{path.name} fields differ: missing={sorted(required - set(raw))}, "
            f"unexpected={sorted(set(raw) - required)}"
        )

    flavor = raw["flavor"]
    if not isinstance(flavor, str) or flavor not in FLAVORS or path.stem != flavor:
        raise FlavorPolicyError(
            f"{path.name} must declare the matching flavor, one of {FLAVORS}"
        )
    expected_march, expected_symbol = _EXPECTED[flavor]
    if raw["compiler_march"] != expected_march:
        raise FlavorPolicyError(f"{flavor} compiler_march must be {expected_march}")
    if raw["rust_target_cpu"] != expected_march:
        raise FlavorPolicyError(f"{flavor} Rust target CPU must match the C baseline")
    if raw["kconfig_symbol"] != expected_symbol:
        raise FlavorPolicyError(f"{flavor} kconfig symbol must be {expected_symbol}")

    cpu_flags = _strings(raw["cpu_flags"], "cpu_flags")
    if any(not _CPU_FLAG_RE.fullmatch(flag) for flag in cpu_flags):
        raise FlavorPolicyError("cpu_flags contains an invalid kernel CPU flag")
    c_no_simd = _strings(raw["c_no_simd_flags"], "c_no_simd_flags")
    if c_no_simd != C_NO_SIMD_FLAGS:
        raise FlavorPolicyError("C no-SIMD flags differ from the reviewed kernel set")
    rust_no_simd = _strings(raw["rust_no_simd_features"], "rust_no_simd_features")
    if rust_no_simd != RUST_NO_SIMD_FEATURES:
        raise FlavorPolicyError("Rust no-SIMD features differ from the reviewed kernel set")

    allowlist_name = raw["intentional_fpu_allowlist"]
    if not isinstance(allowlist_name, str) or pathlib.PurePath(allowlist_name).name != allowlist_name:
        raise FlavorPolicyError("intentional_fpu_allowlist must be a sibling file name")
    allowlist_path = path.parent / allowlist_name
    try:
        allowlist = tomllib.loads(allowlist_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise FlavorPolicyError(f"cannot read {allowlist_path}: {exc}") from exc
    if set(allowlist) != {"schema_version", "source_version", "final_artifact", "objects"}:
        raise FlavorPolicyError("intentional FPU allowlist has unexpected fields")
    if allowlist["schema_version"] != 1 or allowlist["source_version"] != "7.1.7-1":
        raise FlavorPolicyError("intentional FPU allowlist is not pinned to linux 7.1.7-1")
    objects = _strings(allowlist["objects"], "intentional FPU objects")
    artifact = allowlist["final_artifact"]
    if (
        artifact != "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko"
        or pathlib.PurePosixPath(artifact).is_absolute()
        or ".." in pathlib.PurePosixPath(artifact).parts
    ):
        raise FlavorPolicyError("intentional FPU final artifact is not the reviewed amdgpu module")
    if tuple(sorted(objects)) != objects:
        raise FlavorPolicyError("intentional FPU objects must be sorted")
    if any(not _OBJECT_RE.fullmatch(obj) or ".." in pathlib.PurePosixPath(obj).parts for obj in objects):
        raise FlavorPolicyError("intentional FPU allowlist contains an unsafe object path")

    schema = raw["schema_version"]
    if schema != 1:
        raise FlavorPolicyError(f"unsupported flavor policy schema {schema!r}")
    return FlavorPolicy(
        schema_version=schema,
        flavor=flavor,
        compiler_march=expected_march,
        rust_target_cpu=expected_march,
        kconfig_symbol=expected_symbol,
        cpu_flags=cpu_flags,
        c_no_simd_flags=c_no_simd,
        rust_no_simd_features=rust_no_simd,
        intentional_fpu_artifact=artifact,
        intentional_fpu_objects=objects,
    )


def load_all_flavor_policies(directory: pathlib.Path) -> dict[str, FlavorPolicy]:
    policies = {flavor: load_flavor_policy(directory / f"{flavor}.toml") for flavor in FLAVORS}
    for lower, higher in zip(FLAVORS, FLAVORS[1:]):
        missing = set(policies[lower].cpu_flags) - set(policies[higher].cpu_flags)
        if missing:
            raise FlavorPolicyError(
                f"{higher} is not a strict superset of {lower}: missing {sorted(missing)}"
            )
    return policies
