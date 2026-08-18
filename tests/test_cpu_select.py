"""The user-space CPU selector follows the flavor policies exactly."""

from __future__ import annotations

import json
import pathlib
import subprocess

from dkc.flavors import load_all_flavor_policies

ROOT = pathlib.Path(__file__).resolve().parent.parent
SELECTOR = ROOT / "scripts" / "dkc-cpu-select"
FIXTURES = ROOT / "tests" / "fixtures"


def _run(name: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SELECTOR), "--cpuinfo", str(FIXTURES / name), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_selector_requirements_equal_the_build_policies() -> None:
    policies = load_all_flavor_policies(ROOT / "config" / "flavors")
    for flavor, policy in policies.items():
        result = subprocess.run(
            [str(SELECTOR), "--requirements", flavor],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        assert tuple(result.stdout.splitlines()) == policy.cpu_flags


def test_selector_recommends_each_exact_level() -> None:
    for flavor in ("v2", "v3", "v4"):
        result = _run(f"cpuinfo-{flavor}.txt", "--json", "--require", flavor)
        assert result.returncode == 0
        report = json.loads(result.stdout)
        assert report == {
            "schema_version": 1,
            "processors": 2 if flavor == "v2" else 1,
            "recommended": flavor,
            "requested": flavor,
            "compatible": True,
            "missing": [],
        }


def test_selector_rejects_a_higher_requested_level() -> None:
    result = _run("cpuinfo-v2.txt", "--json", "--require", "v3")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["recommended"] == "v2"
    assert report["compatible"] is False
    assert report["missing"] == [
        "abm",
        "avx",
        "avx2",
        "bmi1",
        "bmi2",
        "f16c",
        "fma",
        "movbe",
        "xsave",
    ]


def test_selector_uses_the_intersection_of_all_processors() -> None:
    result = _run("cpuinfo-heterogeneous.txt", "--json")
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["processors"] == 2
    assert report["recommended"] == "v2"


def test_selector_rejects_a_machine_below_v2() -> None:
    result = _run("cpuinfo-v1.txt", "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["recommended"] == "none"
    assert report["compatible"] is False
    assert report["missing"]


def test_selector_rejects_input_without_linux_flags(tmp_path: pathlib.Path) -> None:
    bad = tmp_path / "cpuinfo"
    bad.write_text("processor : 0\n", encoding="utf-8")
    result = subprocess.run(
        [str(SELECTOR), "--cpuinfo", str(bad)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "no usable Linux flags stanzas" in result.stderr
