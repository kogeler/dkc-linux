"""Checks for the bounded exact-source kernel selftest profiles."""

from __future__ import annotations

import pathlib
import shlex

import pytest

from dkc.sourceprofile import KselftestPolicy, load_profiles

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCUMENTATION = ROOT / "docs" / "KERNEL_TESTING.md"
PROFILES = {profile.profile_id: profile for profile in load_profiles(ROOT)}


@pytest.mark.parametrize("profile_id", sorted(PROFILES))
def test_profile_renders_the_complete_runtime_policy(profile_id: str) -> None:
    selftest: KselftestPolicy = PROFILES[profile_id].kselftest
    environment = dict(
        assignment.split("=", 1) for assignment in shlex.split(selftest.environment())
    )
    assert environment == {
        "DKC_KSELFTEST_PROFILE_KIND": selftest.kind,
        "DKC_KSELFTEST_TARGETS": " ".join(selftest.targets),
        "DKC_KSELFTEST_TESTS": " ".join(selftest.tests),
        "DKC_KSELFTEST_V3_TESTS": " ".join(selftest.v3_tests),
        "DKC_KSELFTEST_PER_TEST_TIMEOUT": str(selftest.per_test_timeout),
        "DKC_KSELFTEST_AGGREGATE_TIMEOUT": str(selftest.aggregate_timeout),
        "DKC_KSELFTEST_REQUIRED_BUILTIN": " ".join(selftest.required_builtin),
        "DKC_KSELFTEST_REQUIRED_ENABLED": " ".join(selftest.required_enabled),
    }


@pytest.mark.parametrize("profile_id", sorted(PROFILES))
def test_selected_ptrace_wrapper_exists_and_is_executable(profile_id: str) -> None:
    profile = PROFILES[profile_id]
    tests = set(profile.kselftest.tests)
    if "ptrace:vmaccess-only" not in tests:
        return
    wrapper = ROOT / "tests/integration/kselftest-wrappers/ptrace-vmaccess-only"
    assert wrapper.is_file()
    assert wrapper.stat().st_mode & 0o111


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
