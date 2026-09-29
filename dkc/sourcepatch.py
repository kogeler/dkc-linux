"""Apply a reviewed upstream source patch whose context may have moved.

Selftest fixes are carried as plain unified diffs against one pinned kernel
source. A later Debian upload of the same series often edits lines next to such
a fix, or already contains the fix, and neither should stop the build. The
changed lines stay exact; only their context is relaxed:

- a hunk applies where its complete old text appears exactly once;
- otherwise it applies where its removed lines, together with one complete side
  of its context, appear exactly once;
- a patch whose new text is present and whose old text is not is reported as
  already present and changes nothing;
- anything else fails, including a hunk that matches in several places and a
  patch that is only partly present.

Hunks of one file are placed in order, and nothing is written unless every hunk
of every file has been placed.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
from dataclasses import dataclass

__all__ = [
    "ALREADY_PRESENT",
    "APPLIED",
    "CONTEXT_MOVED",
    "FilePatch",
    "Hunk",
    "SourcePatchError",
    "apply_patch",
    "parse_patch",
]

APPLIED = "applied"
CONTEXT_MOVED = "applied-context-moved"
ALREADY_PRESENT = "already-present"


class SourcePatchError(ValueError):
    """A source patch is unsupported or cannot be placed unambiguously."""


@dataclass(frozen=True)
class Hunk:
    """One hunk as (tag, line) pairs, tag being ' ', '-' or '+'."""

    lines: tuple[tuple[str, str], ...]

    @property
    def old(self) -> tuple[str, ...]:
        return tuple(text for tag, text in self.lines if tag != "+")

    @property
    def new(self) -> tuple[str, ...]:
        return tuple(text for tag, text in self.lines if tag != "-")

    @property
    def leading(self) -> int:
        count = 0
        while self.lines[count][0] == " ":
            count += 1
        return count

    @property
    def trailing(self) -> int:
        count = 0
        while self.lines[-1 - count][0] == " ":
            count += 1
        return count

    def reversed(self) -> Hunk:
        swap = {" ": " ", "-": "+", "+": "-"}
        return Hunk(tuple((swap[tag], text) for tag, text in self.lines))


@dataclass(frozen=True)
class FilePatch:
    path: str
    hunks: tuple[Hunk, ...]

    def reversed(self) -> FilePatch:
        return FilePatch(self.path, tuple(hunk.reversed() for hunk in self.hunks))


_HUNK_HEADER = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
_UNSUPPORTED = (
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
    "new file mode ",
    "deleted file mode ",
    "old mode ",
    "new mode ",
    "Binary files ",
    "GIT binary patch",
)


def _lines(text: str) -> list[str]:
    """Split on newlines only; kernel sources contain form feeds."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def _patch_path(header: str, prefix: str) -> str:
    name = header[4:].rstrip("\n").split("\t", 1)[0]
    if name == "/dev/null":
        raise SourcePatchError("creating or deleting files is not supported")
    if not name.startswith(prefix):
        raise SourcePatchError(f"patch path lacks the {prefix} prefix: {name!r}")
    path = name[len(prefix) :]
    parts = pathlib.PurePosixPath(path).parts
    if not parts or path.startswith("/") or ".." in parts:
        raise SourcePatchError(f"unsafe patch path: {name!r}")
    return path


def _parse_hunk(lines: list[str], index: int, path: str) -> tuple[Hunk, int]:
    match = _HUNK_HEADER.match(lines[index])
    assert match is not None
    old_count = 1 if match[1] is None else int(match[1])
    new_count = 1 if match[2] is None else int(match[2])
    index += 1
    body: list[tuple[str, str]] = []
    old = new = 0
    while old < old_count or new < new_count:
        if index >= len(lines):
            raise SourcePatchError(f"truncated hunk in {path}")
        # As in patch(1), an empty line is blank context whose leading space
        # an editor or mail client removed.
        tag, text = (" ", "\n") if lines[index] == "\n" else (lines[index][:1], lines[index][1:])
        if tag not in (" ", "-", "+") or not text.endswith("\n"):
            raise SourcePatchError(f"patch line {index + 1} is not a complete hunk line")
        old += tag != "+"
        new += tag != "-"
        body.append((tag, text))
        index += 1
    if (old, new) != (old_count, new_count):
        raise SourcePatchError(f"hunk line counts do not match in {path}")
    if index < len(lines) and lines[index].startswith("\\"):
        raise SourcePatchError(f"{path}: a missing final newline is not supported")
    if all(tag == " " for tag, _ in body):
        raise SourcePatchError(f"{path}: a hunk changes nothing")
    return Hunk(tuple(body)), index


def parse_patch(text: str) -> tuple[FilePatch, ...]:
    """Parse the modifications of existing files in one unified diff.

    Descriptive text before a file header is ignored, as patch(1) does, so a
    format-patch preamble is accepted. File creation, deletion, renames, mode
    changes, and binary data are rejected.
    """
    lines = _lines(text)
    files: list[FilePatch] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(_UNSUPPORTED):
            raise SourcePatchError(f"unsupported patch feature: {line.strip()}")
        if not line.startswith("--- "):
            if line.startswith(("+++ ", "@@ ", "\\")):
                raise SourcePatchError(f"patch line {index + 1} is outside a file")
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            raise SourcePatchError(f"patch line {index + 1} lacks its +++ header")
        path = _patch_path(line, "a/")
        if _patch_path(lines[index + 1], "b/") != path:
            raise SourcePatchError(f"renaming {path} is not supported")
        index += 2
        hunks: list[Hunk] = []
        while index < len(lines) and _HUNK_HEADER.match(lines[index]):
            hunk, index = _parse_hunk(lines, index, path)
            hunks.append(hunk)
        if not hunks:
            raise SourcePatchError(f"{path} has no hunks")
        files.append(FilePatch(path, tuple(hunks)))
    if not files:
        raise SourcePatchError("patch contains no file changes")
    if len({item.path for item in files}) != len(files):
        raise SourcePatchError("patch names one file more than once")
    return tuple(files)


def _find(lines: list[str], needle: tuple[str, ...], start: int) -> list[int]:
    width = len(needle)
    first = needle[0]
    return [
        position
        for position in range(start, len(lines) - width + 1)
        if lines[position] == first and tuple(lines[position : position + width]) == needle
    ]


@dataclass(frozen=True)
class _Placement:
    start: int
    length: int
    replacement: tuple[str, ...]
    exact: bool


def _place(lines: list[str], hunk: Hunk, start: int, label: str) -> _Placement:
    old, new = hunk.old, hunk.new
    lead, trail = hunk.leading, hunk.trailing
    core_old = old[lead : len(old) - trail]
    core_new = new[lead : len(new) - trail]
    if not old:
        raise SourcePatchError(f"{label} has no old text to place")
    exact = _find(lines, old, start)
    if len(exact) > 1:
        raise SourcePatchError(f"{label} matches in {len(exact)} places")
    if exact:
        return _Placement(exact[0] + lead, len(core_old), core_new, True)
    # The changed lines with one complete side of context. A side without
    # context lines cannot place a hunk on its own.
    positions: set[int] = set()
    if lead:
        positions.update(
            position + lead for position in _find(lines, old[: len(old) - trail], start)
        )
    if trail:
        positions.update(_find(lines, old[lead:], start))
    if len(positions) != 1:
        problem = "is not present" if not positions else f"matches in {len(positions)} places"
        raise SourcePatchError(f"{label} {problem}")
    return _Placement(positions.pop(), len(core_old), core_new, False)


def _apply_file(lines: list[str], patch: FilePatch) -> tuple[list[str], bool]:
    result = list(lines)
    cursor = 0
    exact = True
    for number, hunk in enumerate(patch.hunks, 1):
        changed = next(text for tag, text in hunk.lines if tag != " ").strip()
        label = f"{patch.path} hunk {number} ({changed[:60]!r})"
        placement = _place(result, hunk, cursor, label)
        result[placement.start : placement.start + placement.length] = placement.replacement
        cursor = placement.start + len(placement.replacement)
        exact = exact and placement.exact
    return result, exact


@dataclass(frozen=True)
class _Attempt:
    files: dict[str, list[str]] | None
    exact: bool
    error: str


def _attempt(originals: dict[str, list[str]], patch: tuple[FilePatch, ...]) -> _Attempt:
    files: dict[str, list[str]] = {}
    exact = True
    for item in patch:
        try:
            files[item.path], file_exact = _apply_file(originals[item.path], item)
        except SourcePatchError as error:
            return _Attempt(None, False, str(error))
        exact = exact and file_exact
    return _Attempt(files, exact, "")


def _read(root: pathlib.Path, path: str) -> list[str]:
    target = root / path
    if target.is_symlink() or not target.is_file():
        raise SourcePatchError(f"{path} is not a regular file in the source tree")
    if not target.resolve().is_relative_to(root.resolve()):
        raise SourcePatchError(f"{path} resolves outside the source tree")
    return _lines(target.read_bytes().decode("utf-8", "surrogateescape"))


def apply_patch(root: pathlib.Path, text: str) -> str:
    """Apply one patch to `root` and return how it was applied."""
    patch = parse_patch(text)
    originals = {item.path: _read(root, item.path) for item in patch}
    forward = _attempt(originals, patch)
    backward = _attempt(originals, tuple(item.reversed() for item in patch))
    if (
        forward.files is not None
        and (forward.exact or backward.files is None)
        and not backward.exact
    ):
        for path, lines in forward.files.items():
            (root / path).write_bytes("".join(lines).encode("utf-8", "surrogateescape"))
        return APPLIED if forward.exact else CONTEXT_MOVED
    if (
        backward.files is not None
        and (backward.exact or forward.files is None)
        and not forward.exact
    ):
        return ALREADY_PRESENT
    if forward.files is not None:
        raise SourcePatchError(
            "the patch both applies and appears to be present already; review the source"
        )
    raise SourcePatchError(f"{forward.error}, and the fix is not already present")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m dkc.sourcepatch",
        description="Apply one reviewed source patch whose context may have moved.",
    )
    parser.add_argument("root", type=pathlib.Path)
    parser.add_argument("patch", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        text = args.patch.read_bytes().decode("utf-8", "surrogateescape")
        result = apply_patch(args.root, text)
    except (OSError, SourcePatchError) as error:
        print(f"{args.patch.name}: {error}", file=sys.stderr)
        return 1
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
