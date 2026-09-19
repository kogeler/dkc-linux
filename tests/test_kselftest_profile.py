"""Checks for the bounded exact-source kernel selftest profiles."""

from __future__ import annotations

import pathlib

import pytest

from dkc.sourceprofile import KselftestPolicy, load_profiles


ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCUMENTATION = ROOT / "docs" / "KERNEL_TESTING.md"
PROFILES = {profile.profile_id: profile for profile in load_profiles(ROOT)}


@pytest.mark.parametrize("profile_id", sorted(PROFILES))
def test_profile_is_explicit_unique_and_collection_complete(profile_id: str) -> None:
    selftest: KselftestPolicy = PROFILES[profile_id].kselftest
    assert selftest.kind == "qualification"
    assert len(selftest.targets) == 25
    assert len(selftest.tests) == 35
    assert {test.split(":", 1)[0] for test in selftest.tests} == set(selftest.targets)
    assert not any("benchmark" in test for test in selftest.tests)
    assert selftest.v3_tests == ("x86:corrupt_xstate_header_64", "x86:avx_64")
    assert set(selftest.v3_tests) < set(selftest.tests)


@pytest.mark.parametrize("profile_id", sorted(PROFILES))
def test_profile_has_independent_runtime_limits_and_config_requirements(
    profile_id: str,
) -> None:
    selftest = PROFILES[profile_id].kselftest
    assert selftest.per_test_timeout == 180
    assert selftest.aggregate_timeout == 900
    assert set(selftest.required_builtin).isdisjoint(selftest.required_enabled)
    assert "X86_64" in selftest.required_builtin
    assert "OVERLAY_FS" in selftest.required_enabled


@pytest.mark.parametrize("profile_id", sorted(PROFILES))
def test_profile_keeps_multiple_x86_interface_checks(profile_id: str) -> None:
    tests = set(PROFILES[profile_id].kselftest.tests)
    assert {
        "x86:avx_64",
        "x86:corrupt_xstate_header_64",
        "x86:sigreturn_64",
    } <= tests


@pytest.mark.parametrize("profile_id", sorted(PROFILES))
def test_profile_keeps_repaired_environment_sensitive_interfaces(profile_id: str) -> None:
    profile = PROFILES[profile_id]
    tests = set(profile.kselftest.tests)
    assert {"core:unshare_test", "ptrace:vmaccess-only", "uevent:uevent_filtering"} <= tests
    assert "ptrace:vmaccess" not in tests
    wrapper = ROOT / "tests/integration/kselftest-wrappers/ptrace-vmaccess-only"
    assert wrapper.is_file()
    assert wrapper.stat().st_mode & 0o111
    # Each series carries exactly the selftest source fixes reviewed for it, so
    # a new series must record its own set here rather than inherit one.
    expected = {
        "7.1": {"0001-uevent-receive-buffer.patch"},
        "7.2": {
            "0001-futex-robust-list-atomic-assert.patch",
            "0002-landlock-audit-make-char-chardev.patch",
        },
    }
    assert {path.name for path in profile.kselftest_patches} == expected[profile_id]


def test_openat2_collection_follows_its_upstream_location() -> None:
    assert "openat2" in PROFILES["7.1"].kselftest.targets
    assert "filesystems/openat2" in PROFILES["7.2"].kselftest.targets


def test_environment_sensitive_interfaces_have_maintainer_documentation() -> None:
    documentation = DOCUMENTATION.read_text(encoding="utf-8")
    for required_text in (
        "make kselftest-flavor",
        "core:unshare_test",
        "fs.nr_open",
        "RLIMIT_NOFILE",
        "ptrace:vmaccess-only",
        "ptrace_attach",
        "begin_new_exec",
        "uevent:uevent_filtering",
        "ENOBUFS",
        "c7fdbc2c2f26",
        "0001-uevent-receive-buffer.patch",
    ):
        assert required_text in documentation
