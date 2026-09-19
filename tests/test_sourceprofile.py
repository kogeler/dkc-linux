"""One reviewed source profile per kernel series, selected exactly."""

from __future__ import annotations

import importlib.util
import pathlib
import shlex
import shutil
import sys

import pytest

from dkc.buildpolicy import build_policy_digest
from dkc.sourceprofile import (
    SourceProfileError,
    load_profile,
    load_profiles,
    select_profile,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _copy_registry(destination: pathlib.Path) -> None:
    for relative in ("config", "dkc", "container", "scripts", "debian-overlay", "tests"):
        shutil.copytree(
            ROOT / relative,
            destination / relative,
            ignore=shutil.ignore_patterns("__pycache__"),
        )


def test_every_profile_is_complete_and_covers_its_reviewed_source() -> None:
    profiles = load_profiles(ROOT)
    assert [profile.profile_id for profile in profiles] == ["7.1", "7.2"]
    for profile in profiles:
        assert profile.covers(profile.reviewed_source_version)
        assert profile.overlay_patches
        assert profile.build_policy_paths()[0] == profile.build_file
        assert profile.validation_policy_paths()[0] == profile.validation_file
        assert profile.fpu.final_artifact.endswith("amdgpu.ko")
        assert profile.simd_allowlist
        assert "nodoc" in profile.build_profiles


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("7.1.7-1", "7.1"),
        ("7.1.13-1", "7.1"),
        ("7.2.6-1", "7.2"),
        ("7.2~rc3-1~exp1", "7.2"),
    ],
)
def test_one_profile_covers_each_known_source_version(version: str, expected: str) -> None:
    assert select_profile(ROOT, version).profile_id == expected


def test_an_unreviewed_series_is_named_rather_than_guessed() -> None:
    with pytest.raises(SourceProfileError, match="no source profile covers linux 7.3.1-1"):
        select_profile(ROOT, "7.3.1-1")
    with pytest.raises(SourceProfileError, match="invalid Debian source version"):
        select_profile(ROOT, "not a version")


def test_build_identity_depends_only_on_the_selected_profile(tmp_path: pathlib.Path) -> None:
    original = build_policy_digest(ROOT, "7.2.6-1")
    _copy_registry(tmp_path)
    assert build_policy_digest(tmp_path, "7.2.6-1") == original
    with (tmp_path / "config/source-profiles/7.1/profile.toml").open("a") as stream:
        stream.write("\n")
    assert build_policy_digest(tmp_path, "7.2.6-1") == original
    assert build_policy_digest(tmp_path, "7.1.13-1") != build_policy_digest(ROOT, "7.1.13-1")


def test_overlay_directories_belong_to_a_profile(tmp_path: pathlib.Path) -> None:
    _copy_registry(tmp_path)
    (tmp_path / "debian-overlay/patches/7.9").mkdir()
    with pytest.raises(SourceProfileError, match="does not belong to a source profile"):
        load_profiles(tmp_path)


def test_a_profile_without_an_overlay_is_rejected(tmp_path: pathlib.Path) -> None:
    _copy_registry(tmp_path)
    for patch in (tmp_path / "debian-overlay/patches/7.2").glob("*.patch"):
        patch.unlink()
    with pytest.raises(SourceProfileError, match="lack overlay patches"):
        load_profiles(tmp_path)


def test_profiles_of_one_series_may_not_overlap(tmp_path: pathlib.Path) -> None:
    _copy_registry(tmp_path)
    split = tmp_path / "config/source-profiles/7.2-b"
    shutil.copytree(tmp_path / "config/source-profiles/7.2", split)
    shutil.copytree(
        tmp_path / "debian-overlay/patches/7.2", tmp_path / "debian-overlay/patches/7.2-b"
    )
    with pytest.raises(SourceProfileError, match="overlap"):
        load_profiles(tmp_path)

    build = split / "profile.toml"
    build.write_text(
        build.read_text(encoding="utf-8").replace(
            'reviewed_source_version = "7.2.6-1"',
            'reviewed_source_version = "7.2.6-1"\nfirst_source_version = "7.2.6-1"',
        ),
        encoding="utf-8",
    )
    original = tmp_path / "config/source-profiles/7.2/profile.toml"
    original.write_text(
        original.read_text(encoding="utf-8").replace(
            'reviewed_source_version = "7.2.6-1"',
            'reviewed_source_version = "7.2.2-1"\nbefore_source_version = "7.2.6-1"',
        ),
        encoding="utf-8",
    )
    assert select_profile(tmp_path, "7.2.6-1").profile_id == "7.2-b"
    assert select_profile(tmp_path, "7.2.2-1").profile_id == "7.2"


def test_a_profile_declaring_another_series_is_rejected(tmp_path: pathlib.Path) -> None:
    _copy_registry(tmp_path)
    build = tmp_path / "config/source-profiles/7.2/profile.toml"
    build.write_text(
        build.read_text(encoding="utf-8").replace(
            'kernel_series = "7.2"', 'kernel_series = "7.1"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(SourceProfileError, match="kernel_series does not match"):
        load_profile(tmp_path / "config/source-profiles/7.2")


def test_rendered_shell_inputs_are_exact() -> None:
    profile = select_profile(ROOT, "7.2.6-1")
    environment = {
        key: shlex.split(value)[0]
        for key, _, value in (
            line.partition("=") for line in profile.kselftest.environment().splitlines()
        )
    }
    assert environment["DKC_KSELFTEST_PROFILE_KIND"] == profile.kselftest.kind
    assert environment["DKC_KSELFTEST_TARGETS"].split() == list(profile.kselftest.targets)
    assert environment["DKC_KSELFTEST_TESTS"].split() == list(profile.kselftest.tests)
    assert environment["DKC_KSELFTEST_PER_TEST_TIMEOUT"] == "180"

    assert profile.build_profiles_file().endswith(
        f'DKC_BUILD_PROFILES="{" ".join(profile.build_profiles)}"\n'
    )
    restriction = profile.architecture_restriction()
    assert restriction.count("[[kernelarch]]") == len(profile.kernel_architectures)
    assert "  name = 'amd64'\n  enable = true" in restriction
    assert "name = 'x86'\nenable = false" not in restriction


def _generator():
    path = ROOT / "scripts" / "in-container" / "generate-overlay-patches.py"
    spec = importlib.util.spec_from_file_location("generate_overlay_patches", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_committed_overlays_are_named_by_the_generator() -> None:
    generator = _generator()
    known = set(generator.PATCHES)
    for profile in load_profiles(ROOT):
        names = {path.name for path in profile.overlay_patches}
        assert names <= known, f"{profile.profile_id} has patches the generator cannot produce"
        assert names, profile.profile_id


def test_every_generation_specific_edit_keeps_its_reviewed_spellings() -> None:
    generator = _generator()
    for name, groups in generator.PATCHES.items():
        for group in groups:
            variants = (
                [item for item in group.groups] if isinstance(group, generator.FileVariants) else [group]
            )
            for path, edits in variants:
                assert path.startswith(("debian/", "arch/")), (name, path)
                for edit in edits:
                    if not isinstance(edit, generator.OneOf):
                        assert isinstance(edit, tuple) and len(edit) == 2
                        continue
                    anchors = [
                        variant.marker if isinstance(variant, generator.Absent) else variant[0]
                        for variant in edit.variants
                    ]
                    assert len(anchors) >= 2
                    assert len(anchors) == len(set(anchors)), (name, path)
