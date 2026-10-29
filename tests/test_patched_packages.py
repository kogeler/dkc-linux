from __future__ import annotations

import importlib.util
import re
import sys
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "container" / "patched-packages.toml"
BUILDER = ROOT / "container" / "build-patched-packages.py"


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_patched_packages", BUILDER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve postponed annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _minimal() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "fix": [
            {
                "id": "example-fix",
                "revision": 1,
                "problem": "Explains the defect.",
                "upstream": "https://example.invalid/fix",
                "install": ["example-bin"],
                "source": [
                    {
                        "package": "example-src",
                        "patch": [
                            {
                                "name": "fix.patch",
                                "diff": "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n",
                            }
                        ],
                    },
                    {"package": "example-cli"},
                ],
            }
        ],
    }


def test_committed_ledger_is_valid() -> None:
    with LEDGER.open("rb") as stream:
        ledger = _builder().parse_ledger(tomllib.load(stream))
    for fix in ledger.fixes:
        assert fix.install
        assert any(source.patches for source in fix.sources)


def test_minimal_ledger_parses_in_build_order() -> None:
    ledger = _builder().parse_ledger(_minimal())
    (fix,) = ledger.fixes
    assert [source.package for source in fix.sources] == ["example-src", "example-cli"]
    assert fix.sources[0].patches[0].name == "fix.patch"
    assert fix.obsolete_from_upstream_version is None


def test_an_empty_ledger_is_valid() -> None:
    ledger = _builder().parse_ledger({"schema_version": 1})
    assert ledger.fixes == ()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(extra=1),
        lambda data: data.update(schema_version=2),
        lambda data: data["fix"][0].update(unknown=1),
        lambda data: data["fix"][0].update(problem=" "),
        lambda data: data["fix"][0].pop("upstream"),
        lambda data: data["fix"][0].update(revision=0),
        lambda data: data["fix"][0].update(install=[]),
        lambda data: data["fix"][0].update(id="Bad Id"),
        lambda data: data["fix"].append(dict(data["fix"][0])),
        lambda data: data["fix"][0]["source"][1].update(package="example-src"),
        lambda data: data["fix"][0]["source"][0]["patch"][0].update(name="../x.patch"),
        lambda data: data["fix"][0]["source"][0]["patch"][0].update(diff="no diff"),
        lambda data: data["fix"][0]["source"][0].pop("patch"),
    ],
    ids=[
        "unknown-top-key",
        "schema",
        "unknown-fix-key",
        "empty-problem",
        "missing-upstream",
        "revision",
        "no-install",
        "unsafe-id",
        "duplicate-id",
        "duplicate-source",
        "unsafe-patch-name",
        "not-a-diff",
        "no-patch-at-all",
    ],
)
def test_malformed_ledgers_are_rejected_before_any_build(mutate) -> None:  # type: ignore[no-untyped-def]
    builder = _builder()
    data = _minimal()
    mutate(data)
    with pytest.raises(builder.LedgerError):
        builder.parse_ledger(data)


@pytest.mark.parametrize(
    ("version", "upstream"),
    [("0.71.1-4", "0.71.1"), ("1:2.3-1+deb13u1", "2.3"), ("5.0", "5.0")],
)
def test_upstream_version_strips_epoch_and_debian_revision(
    version: str, upstream: str
) -> None:
    assert _builder().upstream_version(version) == upstream


def test_ledger_details_appear_nowhere_else() -> None:
    # The ledger is the single place that names a fix, its sources, its
    # patches, and its upstream reference, so deleting a block removes it.
    with LEDGER.open("rb") as stream:
        data = tomllib.load(stream)
    tokens: set[str] = set()
    for fix in data.get("fix", []):
        tokens.add(fix["id"])
        tokens.add(fix["upstream"])
        for source in fix["source"]:
            tokens.add(source["package"])
            tokens.update(patch["name"] for patch in source.get("patch", []))
    leaks = []
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {".git", "out"} or "__pycache__" in relative.parts:
            continue
        if path == LEDGER or path.is_symlink() or not path.is_file():
            continue
        name = relative.as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for token in tokens:
            if re.search(rf"(?<![\w.-]){re.escape(token)}(?![\w-])", text):
                leaks.append(f"{name}: {token}")
    assert leaks == []
