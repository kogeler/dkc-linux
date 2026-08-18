"""Checks for the bounded exact-source kernel selftest profile."""

from __future__ import annotations

import pathlib
import re
import shlex


ROOT = pathlib.Path(__file__).resolve().parent.parent
PROFILE = ROOT / "config" / "kselftest.env"
DOCUMENTATION = ROOT / "docs" / "KERNEL_TESTING.md"


def _profile() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in PROFILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, raw_value = line.partition("=")
        assert separator and re.fullmatch(r"DKC_KSELFTEST_[A-Z0-9_]+", key)
        parsed = shlex.split(raw_value)
        assert len(parsed) == 1
        assert key not in values
        values[key] = parsed[0]
    return values


def test_profile_is_explicit_unique_and_collection_complete() -> None:
    profile = _profile()
    assert profile["DKC_KSELFTEST_PROFILE_KIND"] == "qualification"
    targets = profile["DKC_KSELFTEST_TARGETS"].split()
    tests = profile["DKC_KSELFTEST_TESTS"].split()
    v3_tests = profile["DKC_KSELFTEST_V3_TESTS"].split()

    assert len(targets) == len(set(targets)) == 25
    assert len(tests) == len(set(tests)) == 35
    assert all(
        re.fullmatch(r"[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*", target)
        for target in targets
    )
    assert all(
        re.fullmatch(
            r"[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*:[A-Za-z0-9_.+/-]+",
            test,
        )
        for test in tests
    )
    assert {test.split(":", 1)[0] for test in tests} == set(targets)
    assert not any("benchmark" in test for test in tests)
    assert v3_tests == ["x86:corrupt_xstate_header_64", "x86:avx_64"]
    assert set(v3_tests) < set(tests)


def test_profile_has_independent_runtime_limits_and_config_requirements() -> None:
    profile = _profile()
    assert profile["DKC_KSELFTEST_PER_TEST_TIMEOUT"] == "180"
    assert profile["DKC_KSELFTEST_AGGREGATE_TIMEOUT"] == "900"

    builtin = profile["DKC_KSELFTEST_REQUIRED_BUILTIN"].split()
    enabled = profile["DKC_KSELFTEST_REQUIRED_ENABLED"].split()
    assert len(builtin) == len(set(builtin))
    assert len(enabled) == len(set(enabled))
    assert set(builtin).isdisjoint(enabled)
    assert all(re.fullmatch(r"[A-Z0-9_]+", symbol) for symbol in builtin + enabled)


def test_profile_keeps_multiple_x86_interface_checks() -> None:
    tests = set(_profile()["DKC_KSELFTEST_TESTS"].split())
    assert {
        "x86:avx_64",
        "x86:corrupt_xstate_header_64",
        "x86:sigreturn_64",
    } <= tests


def test_profile_keeps_repaired_environment_sensitive_interfaces() -> None:
    tests = set(_profile()["DKC_KSELFTEST_TESTS"].split())
    assert {
        "core:unshare_test",
        "ptrace:vmaccess-only",
        "uevent:uevent_filtering",
    } <= tests
    assert "ptrace:vmaccess" not in tests
    assert (ROOT / "tests/integration/kselftest-patches/0001-uevent-receive-buffer.patch").is_file()
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
