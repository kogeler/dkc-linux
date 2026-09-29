"""Selftest source patches keep their changed lines exact and tolerate moved context."""

from __future__ import annotations

import pathlib

import pytest

from dkc.sourcepatch import (
    ALREADY_PRESENT,
    APPLIED,
    CONTEXT_MOVED,
    SourcePatchError,
    apply_patch,
    main,
    parse_patch,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
PATH = "tools/testing/selftests/demo/demo_test.c"

SOURCE = "".join(
    [
        *(f"/* filler {number} */\n" for number in range(10)),
        "TEST(first)\n",
        "{\n",
        "\tret = call(futex);\n",
        "\n",
        "\tASSERT_EQ(ret, 0);\n",
        "\tASSERT_EQ(*futex, 0);\n",
        "\n",
        "\t/* Check the lower bits */\n",
        "}\n",
        *(f"/* middle {number} */\n" for number in range(10)),
        "TEST(second)\n",
        "{\n",
        "\tret = call(futex);\n",
        "\n",
        "\tASSERT_EQ(ret, 1);\n",
        "\tASSERT_EQ(*futex, 0);\n",
        "\n",
        "\t/* Check the upper bits */\n",
        "}\n",
    ]
)

# The blank context lines deliberately lack their leading space, as they do
# after an editor strips trailing whitespace; patch(1) accepts that too.
PATCH = f"""--- a/{PATH}
+++ b/{PATH}
@@ -13,7 +13,7 @@
 \tret = call(futex);

 \tASSERT_EQ(ret, 0);
-\tASSERT_EQ(*futex, 0);
+\tASSERT_EQ(atomic_load(futex), 0);

 \t/* Check the lower bits */
 }}
@@ -32,7 +32,7 @@
 \tret = call(futex);

 \tASSERT_EQ(ret, 1);
-\tASSERT_EQ(*futex, 0);
+\tASSERT_EQ(atomic_load(futex), 0);

 \t/* Check the upper bits */
 }}
"""

FIXED = SOURCE.replace("\tASSERT_EQ(*futex, 0);\n", "\tASSERT_EQ(atomic_load(futex), 0);\n")


def _tree(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    target = tmp_path / "tree" / PATH
    target.parent.mkdir(parents=True)
    target.write_text(text, encoding="utf-8")
    return tmp_path / "tree"


def _read(root: pathlib.Path) -> str:
    return (root / PATH).read_text(encoding="utf-8")


def test_exact_patch_applies_each_hunk_at_its_own_place(tmp_path: pathlib.Path) -> None:
    root = _tree(tmp_path, SOURCE)
    assert apply_patch(root, PATCH) == APPLIED
    assert _read(root) == FIXED


def test_moved_context_is_tolerated_when_one_side_still_matches(
    tmp_path: pathlib.Path,
) -> None:
    # A later upload edits the lines before the first fix and after the second
    # one, and shifts everything by a few lines. patch --fuzz=0 rejects both.
    drifted = (
        SOURCE.replace("/* filler 2 */\n", "")
        .replace("\tret = call(futex);\n\n\tASSERT_EQ(ret, 0);", "\tret = call2(futex);\n\n\tASSERT_EQ(ret, 0);")
        .replace("\t/* Check the upper bits */\n", "\t/* Check the upper 32 bits */\n")
    )
    root = _tree(tmp_path, drifted)
    assert apply_patch(root, PATCH) == CONTEXT_MOVED
    assert _read(root) == drifted.replace(
        "\tASSERT_EQ(*futex, 0);\n", "\tASSERT_EQ(atomic_load(futex), 0);\n"
    )


@pytest.mark.parametrize("already", [FIXED, FIXED.replace("/* filler 4 */\n", "")])
def test_a_fix_the_source_already_carries_is_reported_and_left_alone(
    tmp_path: pathlib.Path, already: str
) -> None:
    root = _tree(tmp_path, already)
    assert apply_patch(root, PATCH) == ALREADY_PRESENT
    assert _read(root) == already


def test_a_partly_present_fix_fails_without_writing(tmp_path: pathlib.Path) -> None:
    partial = SOURCE.replace(
        "\tASSERT_EQ(ret, 0);\n\tASSERT_EQ(*futex, 0);\n",
        "\tASSERT_EQ(ret, 0);\n\tASSERT_EQ(atomic_load(futex), 0);\n",
    )
    root = _tree(tmp_path, partial)
    with pytest.raises(SourcePatchError, match="not already present"):
        apply_patch(root, PATCH)
    assert _read(root) == partial


def test_a_changed_removed_line_fails(tmp_path: pathlib.Path) -> None:
    changed = SOURCE.replace("\tASSERT_EQ(*futex, 0);\n", "\tASSERT_EQ(*futex, 0U);\n")
    root = _tree(tmp_path, changed)
    with pytest.raises(SourcePatchError, match="hunk 1 .* is not present"):
        apply_patch(root, PATCH)
    assert _read(root) == changed


def test_context_changed_on_both_sides_fails(tmp_path: pathlib.Path) -> None:
    changed = SOURCE.replace("\tASSERT_EQ(ret, 0);\n", "\tASSERT_EQ(ret, 2);\n").replace(
        "\t/* Check the lower bits */\n", "\t/* Check the low bits */\n"
    )
    root = _tree(tmp_path, changed)
    with pytest.raises(SourcePatchError, match="hunk 1 .* is not present"):
        apply_patch(root, PATCH)


def test_a_hunk_that_matches_in_several_places_fails(tmp_path: pathlib.Path) -> None:
    duplicated = SOURCE.replace(
        "\t/* Check the upper bits */\n", "\t/* Check the lower bits */\n"
    ).replace("\tASSERT_EQ(ret, 1);\n", "\tASSERT_EQ(ret, 0);\n")
    root = _tree(tmp_path, duplicated)
    with pytest.raises(SourcePatchError, match="matches in 2 places"):
        apply_patch(root, PATCH)
    assert _read(root) == duplicated


def test_a_pure_insertion_is_placed_by_its_intact_side(tmp_path: pathlib.Path) -> None:
    source = "a\nb\nc\nd\ne\nf\n"
    patch = "--- a/f\n+++ b/f\n@@ -1,6 +1,7 @@\n a\n b\n c\n+new\n d\n e\n f\n"
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f").write_text(source.replace("a\n", "z\n"), encoding="utf-8")
    assert apply_patch(root, patch) == CONTEXT_MOVED
    assert (root / "f").read_text() == "z\nb\nc\nnew\nd\ne\nf\n"


def test_form_feeds_and_non_utf8_bytes_survive(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f").write_bytes(b"one\n\x0c\ntwo \xff\nthree\n")
    patch = "--- a/f\n+++ b/f\n@@ -2,3 +2,3 @@\n \x0c\n-two \udcff\n+two\n three\n"
    assert apply_patch(root, patch) == APPLIED
    assert (root / "f").read_bytes() == b"one\n\x0c\ntwo\nthree\n"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("--- /dev/null\n+++ b/f\n@@ -0,0 +1 @@\n+x\n", "creating or deleting"),
        ("--- a/../f\n+++ b/../f\n@@ -1 +1 @@\n-x\n+y\n", "unsafe patch path"),
        ("--- a/f\n+++ b/g\n@@ -1 +1 @@\n-x\n+y\n", "renaming"),
        ("diff --git a/f b/f\nold mode 100644\nnew mode 100755\n", "unsupported patch feature"),
        ("--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+y\n\\ No newline at end of file\n", "final newline"),
        ("--- a/f\n+++ b/f\n@@ -1,2 +1,2 @@\n-x\n+y\n", "truncated hunk"),
        ("--- a/f\n+++ b/f\n@@ -1 +1 @@\n x\n", "changes nothing"),
        ("just a description\n", "no file changes"),
    ],
)
def test_unsupported_or_malformed_patches_are_rejected(patch: str, message: str) -> None:
    with pytest.raises(SourcePatchError, match=message):
        parse_patch(patch)


def test_a_format_patch_preamble_is_ignored() -> None:
    patch = (
        "From 173b1bd87308 Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] selftests: fix\n"
        "---\n"
        " f | 2 +-\n"
        "diff --git a/f b/f\n"
        "index 1111111..2222222 100644\n"
        "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+y\n"
    )
    (parsed,) = parse_patch(patch)
    assert parsed.path == "f"


def test_symlinked_targets_are_refused(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    (tmp_path / "outside").write_text("x\n", encoding="utf-8")
    (root / "f").symlink_to(tmp_path / "outside")
    with pytest.raises(SourcePatchError, match="not a regular file"):
        apply_patch(root, "--- a/f\n+++ b/f\n@@ -1 +1 @@\n-x\n+y\n")
    assert (tmp_path / "outside").read_text() == "x\n"


def test_command_line_prints_the_result(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, FIXED)
    patch = tmp_path / "0001-demo.patch"
    patch.write_text(PATCH, encoding="utf-8")
    assert main([str(root), str(patch)]) == 0
    assert capsys.readouterr().out == f"{ALREADY_PRESENT}\n"
    (root / PATH).write_text(SOURCE.replace("*futex, 0)", "*futex, 1)"), encoding="utf-8")
    assert main([str(root), str(patch)]) == 1
    assert "0001-demo.patch: " in capsys.readouterr().err


def test_every_committed_selftest_patch_is_supported() -> None:
    patches = sorted((ROOT / "tests/integration/kselftest-patches").glob("*/*.patch"))
    assert patches
    for patch in patches:
        parsed = parse_patch(patch.read_text(encoding="utf-8"))
        assert all(item.path.startswith("tools/testing/selftests/") for item in parsed)
