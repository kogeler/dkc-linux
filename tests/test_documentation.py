from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^]]+\]\(([^)]+)\)")
FENCE = re.compile(r"^```(?:sh|bash|shell|console)\s*\n(.*?)^```", re.MULTILINE | re.DOTALL)
INLINE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
MAKE = re.compile(r"(?:^|\s)make\s+([A-Za-z0-9][A-Za-z0-9_-]*)")
TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]*):(?:[^=]|$)", re.MULTILINE)


def _tracked_markdown() -> list[Path]:
    documents = list(ROOT.glob("*.md"))
    for directory in (
        ".github",
        "config",
        "debian-overlay",
        "docs",
        "LICENSES",
        "schemas",
        "tests",
    ):
        documents.extend((ROOT / directory).rglob("*.md"))
    return sorted(set(documents))


def _anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match is None:
            continue
        heading = re.sub(r"<[^>]*>", "", match.group(1)).lower()
        heading = re.sub(r"[^\w\- ]", "", heading)
        base = heading.replace(" ", "-")
        count = occurrences.get(base, 0)
        occurrences[base] = count + 1
        anchors.add(base if count == 0 else f"{base}-{count}")
    return anchors


def test_relative_markdown_links_and_anchors_resolve() -> None:
    for document in _tracked_markdown():
        text = document.read_text(encoding="utf-8")
        for raw_destination in LINK.findall(text):
            destination = raw_destination.strip().split(maxsplit=1)[0].strip("<>")
            if destination.startswith(("https://", "http://", "mailto:")):
                continue
            relative, separator, fragment = destination.partition("#")
            target = document if not relative else (document.parent / unquote(relative)).resolve()
            assert target.exists(), f"{document.relative_to(ROOT)} links to missing {relative}"
            assert target.is_file(), f"{document.relative_to(ROOT)} link is not a file: {relative}"
            if separator and fragment:
                assert unquote(fragment) in _anchors(target), (
                    f"{document.relative_to(ROOT)} links to missing anchor "
                    f"{destination}"
                )


def test_documented_project_make_targets_exist() -> None:
    makefiles = [ROOT / "Makefile", *sorted((ROOT / "mk").glob("*.mk"))]
    targets = {
        target
        for makefile in makefiles
        for target in TARGET.findall(makefile.read_text(encoding="utf-8"))
    }
    for document in _tracked_markdown():
        text = document.read_text(encoding="utf-8")
        snippets = FENCE.findall(text) + INLINE.findall(text)
        for snippet in snippets:
            for target in MAKE.findall(snippet):
                assert target in targets, (
                    f"{document.relative_to(ROOT)} names unknown make target {target}"
                )
