#!/usr/bin/env python3
"""Prove that replay ELF transformation preserves every executable section."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import tempfile


SECTION = re.compile(
    r"^\s*\[\s*\d+\]\s+(?P<name>\S+)\s+\S+\s+"
    r"[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(?P<size>[0-9A-Fa-f]+)\s+"
    r"\S+\s+(?P<flags>\S+)"
)


def fail(message: str) -> None:
    raise SystemExit(f"replay ELF verification FAIL: {message}")


def executable_sections(tool: str, path: pathlib.Path) -> list[tuple[str, int]]:
    result = subprocess.run(
        [tool, "--sections", "--wide", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        fail(f"{tool} could not inspect {path}: {result.stderr.strip()}")
    sections: list[tuple[str, int]] = []
    for line in result.stdout.splitlines():
        match = SECTION.match(line)
        if match and "X" in match.group("flags"):
            sections.append((match.group("name"), int(match.group("size"), 16)))
    if not sections or ".text" not in {name for name, _size in sections}:
        fail(f"{path} has no usable executable-section inventory")
    if len({name for name, _size in sections}) != len(sections):
        fail(f"{path} has duplicate executable section names")
    return sections


def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def dump_section(tool: str, elf: pathlib.Path, name: str, output: pathlib.Path) -> None:
    result = subprocess.run(
        [tool, "--dump-section", f"{name}={output}", elf],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        fail(f"{tool} could not extract {name} from {elf}: {result.stderr.strip()}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("original", type=pathlib.Path)
    parser.add_argument("replay", type=pathlib.Path)
    parser.add_argument("report", type=pathlib.Path)
    parser.add_argument("llvm_major", type=int)
    args = parser.parse_args()

    for path in (args.original, args.replay):
        if not path.is_file():
            fail(f"ELF is absent: {path}")
    if args.llvm_major < 1:
        fail("LLVM major is invalid")

    readelf = f"llvm-readelf-{args.llvm_major}"
    objcopy = f"llvm-objcopy-{args.llvm_major}"
    original_sections = executable_sections(readelf, args.original)
    replay_sections = executable_sections(readelf, args.replay)
    if replay_sections != original_sections:
        fail(
            "executable section names or declared sizes differ: "
            f"original={original_sections!r}, replay={replay_sections!r}"
        )

    records: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="dkc-replay-elf-") as temporary_name:
        temporary = pathlib.Path(temporary_name)
        for index, (name, declared_size) in enumerate(original_sections):
            original_dump = temporary / f"{index}.original"
            replay_dump = temporary / f"{index}.replay"
            dump_section(objcopy, args.original, name, original_dump)
            dump_section(objcopy, args.replay, name, replay_dump)
            original_size = original_dump.stat().st_size
            replay_size = replay_dump.stat().st_size
            original_sha256 = digest(original_dump)
            replay_sha256 = digest(replay_dump)
            if (
                original_size != declared_size
                or replay_size != declared_size
                or replay_sha256 != original_sha256
            ):
                fail(f"executable section differs after replay transform: {name}")
            records.append(
                {"name": name, "size": declared_size, "sha256": original_sha256}
            )

    document = {
        "schema_version": 1,
        "status": "PASS",
        "llvm_major": args.llvm_major,
        "sections": records,
    }
    args.report.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"replay ELF verification PASS: {len(records)} executable sections")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
