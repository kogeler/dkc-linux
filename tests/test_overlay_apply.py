"""The packaging overlay is applied by anchored edit and checked against its patches."""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
from types import ModuleType

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_generator() -> ModuleType:
    path = ROOT / "scripts" / "in-container" / "generate-overlay-patches.py"
    spec = importlib.util.spec_from_file_location("overlay_generator_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()

FILLER = "".join(f"# unrelated line {number}\n" for number in range(12))
REVIEWED_MAKEFILE = (
    FILLER
    + "ifdef CONFIG_X86_NATIVE_CPU\n"
    "        KBUILD_CFLAGS += -march=native\n"
    "        KBUILD_RUSTFLAGS += -Ctarget-cpu=native\n"
    "else\n"
    "        KBUILD_CFLAGS += -march=x86-64 -mtune=generic\n"
    "endif\n"
    "\n"
    "        KBUILD_CFLAGS += -mno-red-zone\n"
    + FILLER
)
# The shape of the Linux 7.2.8 stable update: new lines inside the native branch
# that DKC does not edit, within the three lines of context of the reviewed patch.
DRIFTED_MAKEFILE = REVIEWED_MAKEFILE.replace(
    "        KBUILD_CFLAGS += -march=native\n",
    "        KBUILD_CFLAGS += -march=native\n"
    "        KBUILD_CFLAGS += $(call cc-option,-mno-apx-features=egpr)\n"
    "\n"
    "        # generate_rust_target.rs handles Rust APX gating.\n",
).replace("# unrelated line 3\n", "")


@pytest.fixture
def series(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        GENERATOR,
        "PATCHES",
        {
            "0001-baseline.patch": [
                (
                    "arch/x86/Makefile",
                    [
                        (
                            "else\n"
                            "        KBUILD_CFLAGS += -march=x86-64 -mtune=generic\n"
                            "endif\n",
                            "else\n"
                            "        KBUILD_CFLAGS += -march=$(DKC_TARGET) -mtune=generic\n"
                            "endif\n",
                        )
                    ],
                )
            ],
            "0002-defines.patch": [
                (
                    "debian/config/defines.toml",
                    [
                        GENERATOR.LinePattern(
                            r"c_compiler = 'gcc-[0-9]+'",
                            "@LINE@llvm_major = @LLVM_MAJOR@\n",
                        )
                    ],
                )
            ],
        },
    )
    monkeypatch.setattr(
        GENERATOR,
        "NEW_FILES",
        {"0001-baseline.patch": {"debian/config/amd64/config.v3": "CONFIG_V3=y\n"}},
    )


def _tree(
    root: pathlib.Path, makefile: str = REVIEWED_MAKEFILE, compiler: str = "gcc-16"
) -> pathlib.Path:
    (root / "arch/x86").mkdir(parents=True)
    (root / "arch/x86/Makefile").write_text(makefile, encoding="utf-8")
    (root / "debian/config").mkdir(parents=True)
    (root / "debian/config/defines.toml").write_text(
        f"[build]\nc_compiler = '{compiler}'\nrust_build_depends = []\n", encoding="utf-8"
    )
    return root


def _snapshot(root: pathlib.Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _committed(tmp_path: pathlib.Path) -> pathlib.Path:
    reviewed = _tree(tmp_path / "reviewed")
    patches = tmp_path / "patches"
    patches.mkdir()
    for name, patch in GENERATOR.generate(reviewed, 21).items():
        (patches / name).write_text(patch, encoding="utf-8")
    return patches


@pytest.mark.usefixtures("series")
def test_apply_tolerates_changes_next_to_the_edited_lines(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    patches = _committed(tmp_path)
    target = _tree(tmp_path / "target", DRIFTED_MAKEFILE, compiler="gcc-17")

    GENERATOR.apply(target, 21, patches)

    makefile = (target / "arch/x86/Makefile").read_text(encoding="utf-8")
    assert makefile == DRIFTED_MAKEFILE.replace("-march=x86-64 ", "-march=$(DKC_TARGET) ")
    assert (target / "debian/config/defines.toml").read_text(encoding="utf-8") == (
        "[build]\nc_compiler = 'gcc-17'\nllvm_major = 21\nrust_build_depends = []\n"
    )
    assert (target / "debian/config/amd64/config.v3").read_text() == "CONFIG_V3=y\n"
    # The reviewed patch no longer matches byte for byte; the build still
    # proceeds because only its context and positions moved.
    regenerated = GENERATOR.generate(_tree(tmp_path / "again", DRIFTED_MAKEFILE, "gcc-17"), 21)
    assert regenerated["0001-baseline.patch"] != (patches / "0001-baseline.patch").read_text()
    notes = capsys.readouterr().err
    assert "0001-baseline.patch (context differs from the reviewed source" in notes
    assert "0002-defines.patch (context differs from the reviewed source" in notes


@pytest.mark.usefixtures("series")
def test_apply_rejects_an_upstream_change_to_an_edited_line(tmp_path: pathlib.Path) -> None:
    patches = _committed(tmp_path)
    target = _tree(
        tmp_path / "target",
        REVIEWED_MAKEFILE.replace("-mtune=generic\n", "-mtune=generic -mno-apx\n"),
    )
    before = _snapshot(target)

    with pytest.raises(SystemExit, match="anchor no longer matches exactly once in arch/x86/Makefile"):
        GENERATOR.apply(target, 21, patches)
    assert _snapshot(target) == before


@pytest.mark.usefixtures("series")
def test_apply_rejects_a_committed_patch_the_generator_no_longer_produces(
    tmp_path: pathlib.Path,
) -> None:
    patches = _committed(tmp_path)
    stale = patches / "0001-baseline.patch"
    stale.write_text(
        stale.read_text().replace("-march=$(DKC_TARGET)", "-march=$(OLD_TARGET)"),
        encoding="utf-8",
    )
    target = _tree(tmp_path / "target")
    before = _snapshot(target)

    with pytest.raises(SystemExit, match="0001-baseline.patch does not describe") as error:
        GENERATOR.apply(target, 21, patches)
    assert "-+        KBUILD_CFLAGS += -march=$(OLD_TARGET) -mtune=generic" in str(error.value)
    assert "++        KBUILD_CFLAGS += -march=$(DKC_TARGET) -mtune=generic" in str(error.value)
    assert _snapshot(target) == before


@pytest.mark.usefixtures("series")
def test_apply_rejects_a_different_llvm_major(tmp_path: pathlib.Path) -> None:
    patches = _committed(tmp_path)
    target = _tree(tmp_path / "target")

    with pytest.raises(SystemExit, match="0002-defines.patch does not describe .* LLVM 22"):
        GENERATOR.apply(target, 22, patches)


@pytest.mark.usefixtures("series")
def test_apply_requires_exactly_the_generated_series(tmp_path: pathlib.Path) -> None:
    patches = _committed(tmp_path)
    (patches / "0002-defines.patch").rename(patches / "0009-renamed.patch")
    target = _tree(tmp_path / "target")

    with pytest.raises(
        SystemExit,
        match=re.escape("missing=['0002-defines.patch'], unexpected=['0009-renamed.patch']"),
    ):
        GENERATOR.apply(target, 21, patches)


def test_change_groups_ignore_positions_and_context_but_not_changed_lines() -> None:
    before = "a\nb\n-- removed marker\nc\nd\n"
    after = "a\nb\nadded\nc\nd\n"
    shifted = "".join(f"x{number}\n" for number in range(20))
    reviewed = GENERATOR.unified_diff("f", before, after)
    moved = GENERATOR.unified_diff("f", shifted + "z\n" + before, shifted + "z\n" + after)

    # A removed line that itself starts with "-- " must not read as a header.
    assert "\n--- removed marker\n" in reviewed
    assert GENERATOR.change_groups(reviewed) == [
        ("a/f b/f", ("-- removed marker\n",), ("added\n",))
    ]
    assert reviewed != moved
    assert GENERATOR.change_groups(moved) == GENERATOR.change_groups(reviewed)
    changed = GENERATOR.unified_diff("f", before, after.replace("added", "other"))
    assert GENERATOR.change_groups(changed) != GENERATOR.change_groups(reviewed)
    with pytest.raises(ValueError, match="hunk line counts|truncated hunk"):
        GENERATOR.change_groups(reviewed.replace("@@ -1,5 +1,5 @@", "@@ -1,6 +1,5 @@"))


def test_line_patterns_match_exactly_one_whole_line() -> None:
    edit = GENERATOR.DEFINES[1][0]
    assert isinstance(edit, GENERATOR.LinePattern)
    for compiler in ("gcc-15", "gcc-16", "gcc-17"):
        text = f"[build]\nc_compiler = '{compiler}'\nx = 1\n"
        assert GENERATOR.apply_edit("defines.toml", text, edit, 21) == (
            f"[build]\nc_compiler = '{compiler}'\nllvm_major = 21\nx = 1\n"
        )
    with pytest.raises(SystemExit, match="is ambiguous"):
        GENERATOR.apply_edit(
            "defines.toml", "c_compiler = 'gcc-16'\nc_compiler = 'gcc-17'\n", edit, 21
        )
    for text in ("# c_compiler = 'gcc-16'\n", "c_compiler = 'clang-21'\n"):
        with pytest.raises(SystemExit, match="no longer matches exactly one line"):
            GENERATOR.apply_edit("defines.toml", text, edit, 21)


def _unchanged_edges(anchor: str, replacement: str) -> int:
    old = anchor.splitlines(keepends=True)
    new = replacement.splitlines(keepends=True)
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < min(len(old), len(new)) - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    return prefix + suffix


def test_upstream_kernel_anchors_pin_only_the_lines_they_change() -> None:
    """Stable updates edit arch/x86 every few weeks; anchors must not span them."""

    checked = 0
    for groups in GENERATOR.PATCHES.values():
        for group in groups:
            items = group.groups if isinstance(group, GENERATOR.FileVariants) else [group]
            for path, edits in items:
                if not path.startswith("arch/"):
                    continue
                for edit in edits:
                    variants = edit.variants if isinstance(edit, GENERATOR.OneOf) else (edit,)
                    for anchor, replacement in variants:
                        assert _unchanged_edges(anchor, replacement) <= 1, (path, anchor)
                        checked += 1
    assert checked >= 4


def test_every_committed_overlay_is_a_readable_unified_diff() -> None:
    patches = sorted((ROOT / "debian-overlay" / "patches").glob("*/*.patch"))
    assert patches
    for patch in patches:
        groups = GENERATOR.change_groups(patch.read_text(encoding="utf-8"))
        assert groups, patch
