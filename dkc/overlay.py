"""The anchored-edit engine behind the DKC packaging overlay.

The overlay is defined as exact anchored edits in `debian-overlay/edits.py`,
and the `.patch` files under `debian-overlay/patches/<source-profile>/` are
their output. Both are committed: the patches are what a reviewer reads, the
edits are what regenerates them for every supported Debian packaging
generation. The edits quote and modify GPL-licensed Debian and Linux text and
are kept apart from this engine, which only applies them.

That matters because the anchors are the revalidation trigger. If Debian changes
one of these lines, generation fails loudly with the anchor that no longer
matches, instead of a patch applying with fuzz into something subtly different.

An edit that Debian spells differently in different packaging generations is a
`OneOf` of reviewed spellings. Exactly one spelling must match; none or several
is an error. `Absent` names a generation in which the edit has nothing to do.
Keeping every reviewed spelling lets one overlay regenerate the patches of an
older kernel series after a newer one has been added.

Patches form an ordered series and each one is generated against the result of
the previous ones, exactly as `patch` applies them.

An anchor holds only the lines an edit changes, plus the fewest unchanged lines
that place an insertion. Neighbouring text is not reviewed policy: upstream
stable updates routinely add lines next to an edit, and an anchor that spans
them turns every such update into a failed build. A value Debian changes on its
own schedule that the overlay does not depend on, such as the GCC version it
selects, is a `LinePattern` over the reviewed shape of its line.

Builds therefore apply the anchored edits (`apply`) instead of replaying the
committed patches, whose three lines of context would reject any neighbouring
change. `apply` requires every committed patch to describe exactly the lines
the overlay changes in the tree being built. Context and hunk positions may
differ from the reviewed source; a different changed line may not, so a stale
patch or a different Debian spelling still stops the build.
"""

from __future__ import annotations

import difflib
import importlib.util
import pathlib
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

EDITS = pathlib.Path(__file__).resolve().parent.parent / "debian-overlay" / "edits.py"


@dataclass(frozen=True)
class Absent:
    """A generation in which `marker` does not exist, so the edit is a no-op."""

    marker: str


class OneOf:
    """Reviewed spellings of one edit across Debian packaging generations."""

    def __init__(self, *variants: Variant) -> None:
        if len(variants) < 2:
            raise ValueError("OneOf needs at least two reviewed spellings")
        self.variants = variants


@dataclass(frozen=True)
class LinePattern:
    """One whole line whose reviewed shape, not its exact value, is the anchor.

    For a value Debian changes on its own schedule that the overlay does not
    depend on. `regex` must match exactly one complete line; `@LINE@` in the
    replacement stands for that line, including its newline.
    """

    regex: str
    replacement: str


class FileVariants:
    """One logical file that Debian renamed between packaging generations."""

    def __init__(self, *groups: FileEdits) -> None:
        if len(groups) < 2:
            raise ValueError("FileVariants needs at least two reviewed file names")
        self.groups = groups


Variant = tuple[str, str] | Absent
Edit = tuple[str, str] | OneOf | LinePattern
FileEdits = tuple[str, Sequence[Edit]]
Group = FileEdits | FileVariants


@dataclass(frozen=True)
class Overlay:
    """The ordered patch series and the new files each patch adds."""

    patches: Mapping[str, Sequence[Group]]
    new_files: Mapping[str, Mapping[str, str]]
    llvm_tools: Sequence[tuple[str, str]]


def load_overlay(path: pathlib.Path = EDITS) -> Overlay:
    """Load the reviewed overlay definition from its data module."""
    spec = importlib.util.spec_from_file_location("dkc_overlay_edits", path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load the overlay definition {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    overlay = getattr(module, "OVERLAY", None)
    if not isinstance(overlay, Overlay):
        raise SystemExit(f"{path} does not define an Overlay named OVERLAY")
    return overlay


# Committed patches carry this marker; `apply` renders the selected major.
LLVM_PLACEHOLDER = "@LLVM_MAJOR@"


def kernelvariables_block(
    llvm_major: str, llvm_tools: Sequence[tuple[str, str]]
) -> str:
    lines = []
    for name, value in llvm_tools:
        rendered = value.replace(LLVM_PLACEHOLDER, llvm_major)
        lines.append(f"\techo '{name} = {rendered}' >> '$(DIR)/.kernelvariables'\n")
    return "".join(lines)


def _variant_matches(text: str, variant: tuple[str, str] | Absent) -> bool:
    if isinstance(variant, Absent):
        return variant.marker not in text
    return text.count(variant[0]) == 1


def _render(replacement: str, llvm_major: str, kernelvariables: str) -> str:
    # Explicit markers rather than str.format: the replacements contain
    # literal braces from the Python and Make code they insert, which
    # format() would try to interpret as fields.
    return replacement.replace("@KERNELVARIABLES@", kernelvariables).replace(
        LLVM_PLACEHOLDER, llvm_major
    )


def _apply_line_pattern(
    path: str, text: str, edit: LinePattern, llvm_major: str, kernelvariables: str
) -> str:
    matches = list(re.finditer(rf"^(?:{edit.regex})\n", text, re.MULTILINE))
    if len(matches) != 1:
        problem = (
            "no longer matches exactly one line" if not matches else "is ambiguous"
        )
        raise SystemExit(
            f"line pattern {problem} in {path}; the Debian source changed and "
            f"the overlay must be reviewed:\n---\n{edit.regex}\n---"
        )
    match = matches[0]
    rendered = _render(edit.replacement, llvm_major, kernelvariables).replace(
        "@LINE@", match[0]
    )
    return text[: match.start()] + rendered + text[match.end() :]


def apply_edit(
    path: str, text: str, edit: Edit, llvm_major: str, kernelvariables: str = ""
) -> str:
    """Apply one exact edit, choosing the single matching reviewed spelling."""
    if isinstance(edit, LinePattern):
        return _apply_line_pattern(path, text, edit, llvm_major, kernelvariables)
    variants: tuple[Variant, ...] = (
        edit.variants if isinstance(edit, OneOf) else (edit,)
    )
    matches = [variant for variant in variants if _variant_matches(text, variant)]
    if len(matches) != 1:
        first = variants[0]
        anchor = first.marker if isinstance(first, Absent) else first[0]
        problem = "no longer matches exactly once" if not matches else "is ambiguous"
        raise SystemExit(
            f"anchor {problem} in {path}; the Debian source changed and the "
            f"overlay must be reviewed:\n---\n{anchor[:200]}\n---"
        )
    variant = matches[0]
    if isinstance(variant, Absent):
        return text
    anchor, replacement = variant
    return text.replace(anchor, _render(replacement, llvm_major, kernelvariables), 1)


def unified_diff(path: str, before: str | None, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            [] if before is None else before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="/dev/null" if before is None else f"a/{path}",
            tofile=f"b/{path}",
        )
    )


@dataclass(frozen=True)
class EditedTree:
    """The ordered series applied in memory to one source tree."""

    patches: dict[str, str]
    original: dict[str, str | None]
    files: dict[str, str]


def edit_tree(root: pathlib.Path, llvm_major: str, overlay: Overlay) -> EditedTree:
    """Apply every edit of the ordered series in memory."""
    kernelvariables = kernelvariables_block(llvm_major, overlay.llvm_tools)
    tree: dict[str, str] = {}
    original: dict[str, str | None] = {}

    def current(path: str) -> str:
        if path not in tree:
            # No newline translation: an applied file keeps every byte that
            # no edit touches.
            tree[path] = (root / path).read_bytes().decode("utf-8")
            original[path] = tree[path]
        return tree[path]

    patches: dict[str, str] = {}
    for name, groups in overlay.patches.items():
        chunks = []
        for group in groups:
            selected: FileEdits
            if isinstance(group, FileVariants):
                present = [item for item in group.groups if (root / item[0]).is_file()]
                if len(present) != 1:
                    names = ", ".join(item[0] for item in group.groups)
                    raise SystemExit(
                        f"exactly one reviewed file name must exist: {names}"
                    )
                selected = present[0]
            else:
                selected = group
            path, edits = selected
            before = current(path)
            after = before
            for edit in edits:
                after = apply_edit(path, after, edit, llvm_major, kernelvariables)
            tree[path] = after
            chunks.append(unified_diff(path, before, after))
        for path, content in sorted(overlay.new_files.get(name, {}).items()):
            if (root / path).exists() or path in tree:
                raise SystemExit(
                    f"new overlay file {path} now exists upstream; review the collision"
                )
            tree[path] = content
            original[path] = None
            chunks.append(unified_diff(path, None, content))
        if patch := "".join(chunks):
            patches[name] = patch
    return EditedTree(patches, original, tree)


def generate(root: pathlib.Path, overlay: Overlay) -> dict[str, str]:
    """Return every non-empty patch of the ordered series for one source tree.

    The patches carry LLVM_PLACEHOLDER instead of an LLVM major: the major is
    selected in one place and rendered only by `apply`.
    """
    return edit_tree(root, LLVM_PLACEHOLDER, overlay).patches


_HUNK_HEADER = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")

ChangeGroup = tuple[str, tuple[str, ...], tuple[str, ...]]


def change_groups(patch: str) -> list[ChangeGroup]:
    """Reduce a unified diff to its removed and added lines, in order.

    Hunk positions and context lines are dropped, and every hunk is re-diffed on
    its own, so two diffs of the same edits compare equal even when the text
    around an edit, and with it difflib's alignment, differs between sources.
    """
    lines = patch.splitlines(keepends=True)
    groups: list[ChangeGroup] = []
    index = 0
    while index < len(lines):
        if not (
            lines[index].startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            raise ValueError(f"diff line {index + 1} is not a file header")
        header = f"{lines[index][4:].rstrip()} {lines[index + 1][4:].rstrip()}"
        index += 2
        while index < len(lines) and (match := _HUNK_HEADER.match(lines[index])):
            old_count = 1 if match[1] is None else int(match[1])
            new_count = 1 if match[2] is None else int(match[2])
            index += 1
            old: list[str] = []
            new: list[str] = []
            while len(old) < old_count or len(new) < new_count:
                if index >= len(lines):
                    raise ValueError(f"truncated hunk in {header}")
                tag, text = lines[index][:1], lines[index][1:]
                if tag not in (" ", "-", "+"):
                    raise ValueError(f"diff line {index + 1} is not a hunk line")
                if tag != "+":
                    old.append(text)
                if tag != "-":
                    new.append(text)
                index += 1
            if (len(old), len(new)) != (old_count, new_count):
                raise ValueError(f"hunk line counts do not match in {header}")
            matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
            groups.extend(
                (header, tuple(old[i1:i2]), tuple(new[j1:j2]))
                for tag, i1, i2, j1, j2 in matcher.get_opcodes()
                if tag != "equal"
            )
    return groups


def _render_groups(groups: list[ChangeGroup]) -> list[str]:
    lines = []
    for header, old, new in groups:
        lines.append(f"@ {header}\n")
        lines.extend(f"-{line}" for line in old)
        lines.extend(f"+{line}" for line in new)
    return [line if line.endswith("\n") else line + "\n" for line in lines]


def apply(
    root: pathlib.Path, llvm_major: int, reviewed: pathlib.Path, overlay: Overlay
) -> None:
    """Edit `root` in place, but only as the committed patches describe."""
    edited = edit_tree(root, str(llvm_major), overlay)
    committed = {
        path.name: path.read_bytes()
        .decode("utf-8")
        .replace(LLVM_PLACEHOLDER, str(llvm_major))
        for path in sorted(reviewed.glob("*.patch"))
    }
    if set(committed) != set(edited.patches):
        raise SystemExit(
            "the committed overlay is not the series this source needs: "
            f"missing={sorted(set(edited.patches) - set(committed))}, "
            f"unexpected={sorted(set(committed) - set(edited.patches))}; "
            "regenerate it with `make overlay-patches` and review the result"
        )
    notes = []
    for name, patch in edited.patches.items():
        try:
            expected = change_groups(committed[name])
        except ValueError as error:
            raise SystemExit(
                f"committed {name} is not a readable unified diff: {error}"
            ) from error
        actual = change_groups(patch)
        if expected != actual:
            difference = list(
                difflib.unified_diff(
                    _render_groups(expected),
                    _render_groups(actual),
                    f"committed/{name}",
                    f"generated/{name}",
                    n=1,
                )
            )
            raise SystemExit(
                f"{name} does not describe the lines the overlay changes in this "
                f"source with LLVM {llvm_major}; regenerate it with "
                "`make overlay-patches` and review the difference:\n"
                + "".join(difference[:80])
                + ("[difference truncated]\n" if len(difference) > 80 else "")
            )
        drifted = patch != committed[name]
        notes.append(
            f"  {name}"
            + (
                " (context differs from the reviewed source; changed lines match)"
                if drifted
                else ""
            )
        )
    for path, content in edited.files.items():
        if content == edited.original[path]:
            continue
        target = root / path
        if target.is_symlink():
            raise SystemExit(f"refusing to write the overlay through symlink {path}")
        if edited.original[path] is None:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    print("\n".join(notes), file=sys.stderr)
