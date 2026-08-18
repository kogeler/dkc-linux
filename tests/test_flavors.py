"""The three build policies are exact, nested, and fail closed."""

from __future__ import annotations

import pathlib

import pytest

from dkc.flavors import (
    C_NO_SIMD_FLAGS,
    FlavorPolicyError,
    load_all_flavor_policies,
    load_flavor_policy,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
POLICY_DIR = ROOT / "config" / "flavors"


def test_all_flavor_policies_are_valid_and_nested() -> None:
    policies = load_all_flavor_policies(POLICY_DIR)
    assert tuple(policies) == ("v2", "v3", "v4")
    for flavor, policy in policies.items():
        assert policy.compiler_march == f"x86-64-{flavor}"
        assert policy.rust_target_cpu == policy.compiler_march
        assert policy.c_no_simd_flags == C_NO_SIMD_FLAGS
        assert policy.intentional_fpu_artifact == "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko"
        assert policy.rust_target_feature_flag.startswith("-Ctarget-feature=-sse,")
        assert len(policy.intentional_fpu_objects) == 66


def test_policy_file_name_and_declared_flavor_cannot_disagree(tmp_path: pathlib.Path) -> None:
    text = (POLICY_DIR / "v2.toml").read_text().replace('flavor = "v2"', 'flavor = "v3"')
    path = tmp_path / "v2.toml"
    path.write_text(text)
    with pytest.raises(FlavorPolicyError, match="matching flavor"):
        load_flavor_policy(path)


def test_no_simd_set_cannot_be_weakened(tmp_path: pathlib.Path) -> None:
    text = (POLICY_DIR / "v2.toml").read_text().replace(', "-mno-avx"', "")
    path = tmp_path / "v2.toml"
    path.write_text(text)
    (tmp_path / "intentional-fpu-objects.toml").write_text(
        (POLICY_DIR / "intentional-fpu-objects.toml").read_text()
    )
    with pytest.raises(FlavorPolicyError, match="no-SIMD"):
        load_flavor_policy(path)
