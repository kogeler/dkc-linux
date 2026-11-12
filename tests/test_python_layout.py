"""Invariants of the dkc package layout that its callers depend on."""

from __future__ import annotations

import ast
import pathlib
import re
import sys

import pytest

from dkc.buildpolicy import build_policy_paths
from dkc.sourceprofile import load_profiles
from dkc.validationpolicy import validation_policy_paths

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "dkc" / "cli"
VERSIONS = sorted(profile.reviewed_source_version for profile in load_profiles(ROOT))
SHELL_MODULE = re.compile(r"python3 -m (dkc(?:\.\w+)+)")


def _module_path(name: str) -> pathlib.Path | None:
    base = ROOT.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _package_inits(path: pathlib.Path) -> set[pathlib.Path]:
    inits = set()
    directory = path.parent
    while directory != ROOT:
        inits.add(directory / "__init__.py")
        directory = directory.parent
    return inits


def _imported_modules(path: pathlib.Path) -> set[str]:
    package = ".".join(path.relative_to(ROOT).with_suffix("").parts[:-1])
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parent = package.split(".")[: len(package.split(".")) - node.level + 1]
                base = ".".join(part for part in [*parent, node.module or ""] if part)
            else:
                base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    return {name for name in names if name.split(".")[0] == "dkc"}


def _closure(path: pathlib.Path) -> set[pathlib.Path]:
    seen = {path, *_package_inits(path)}
    pending = [path]
    while pending:
        for name in _imported_modules(pending.pop()):
            target = _module_path(name)
            if target is not None and target not in seen:
                seen.add(target)
                seen.update(_package_inits(target))
                pending.append(target)
    return seen


def _python_run_by(path: pathlib.Path) -> set[pathlib.Path]:
    """Every module file that one policy entry point imports or runs, transitively.

    Library modules are hashed for what they define; only entry points and the
    scripts that call them execute on behalf of a result.
    """
    if path.suffix == ".py":
        return _closure(path) if path.parent == CLI else set()
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return set()
    modules = set()
    for name in SHELL_MODULE.findall(text):
        module = _module_path(name)
        assert module is not None, f"{path.name} runs missing module {name}"
        modules |= _closure(module)
    return modules


def _relative(paths: set[pathlib.Path]) -> list[str]:
    return sorted(path.relative_to(ROOT).as_posix() for path in paths)


@pytest.mark.parametrize("version", VERSIONS)
def test_policy_inputs_hash_every_python_module_they_run(version: str) -> None:
    # A policy digest invalidates accepted results only if every byte that
    # runs on their behalf is hashed by one of the two policies.
    hashed = set(build_policy_paths(ROOT, version)) | set(
        validation_policy_paths(ROOT, version)
    )
    for path in sorted(hashed):
        missing = _python_run_by(path) - hashed
        assert not missing, f"{path.name} runs unhashed {_relative(missing)}"


@pytest.mark.parametrize("version", VERSIONS)
def test_source_package_scripts_run_only_embedded_python(version: str) -> None:
    # The published source package rebuilds a flavor from the build-policy
    # copy embedded in it, without this repository.
    build = set(build_policy_paths(ROOT, version))
    for path in sorted(build):
        if path.is_relative_to(ROOT / "debian-overlay" / "source"):
            missing = _python_run_by(path) - build
            assert not missing, f"{path.name} runs unembedded {_relative(missing)}"


COMMAND_START = re.compile(
    r"(?:;|&&|\|\||\||\$\(|\(|(?<!\$)\{|`|!|\b(?:then|do|else|if|elif|while|until|exec)\b)"
)
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=\S*")


def test_shell_python_path_assignments_start_their_command() -> None:
    # After a wrapper such as `timeout`, `PYTHONPATH=x python3` names a program
    # called "PYTHONPATH=x"; a wrapped command has to say `env`.
    scripts = [
        path
        for path in sorted((ROOT / "scripts").rglob("*"))
        if path.is_file() and path.suffix != ".py"
    ] + sorted((ROOT / "debian-overlay" / "source").glob("*"))
    misplaced = []
    for path in scripts:
        text = path.read_text(encoding="utf-8").replace("\\\n", " ")
        for line in text.splitlines():
            for match in re.finditer(r"\bPYTHONPATH=", line):
                words = COMMAND_START.split(line[: match.start()])[-1].split()
                while words and ASSIGNMENT.fullmatch(words[-1]):
                    words.pop()
                if words and words[-1] not in ("env", "export"):
                    misplaced.append(f"{path.relative_to(ROOT)}: {line.strip()}")
    assert not misplaced


@pytest.mark.parametrize("name", ("prepare_flavor", "normalize_quilt_patch"))
def test_standalone_entry_points_need_only_the_standard_library(name: str) -> None:
    # prepare_flavor is copied into the published source package and
    # normalize_quilt_patch is run directly as dpkg-source's editor; neither
    # can import the dkc package.
    tree = ast.parse((CLI / f"{name}.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


def test_entry_point_names_never_shadow_the_standard_library() -> None:
    # A module run as a plain file puts dkc/cli first on sys.path.
    names = {path.stem for path in CLI.glob("*.py")}
    assert not names & set(sys.stdlib_module_names)
