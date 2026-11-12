"""Fast tests for executable-section preservation checks."""

from __future__ import annotations

import pathlib
from types import SimpleNamespace

import pytest

from dkc.cli import verify_replay_elf as VERIFY

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "dkc" / "cli" / "verify_replay_elf.py"


def test_executable_section_parser_selects_every_x_section(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = """There are 4 section headers:
  [ 1] .text             PROGBITS 0000000000000000 000040 000120 00  AX  0 0 16
  [ 2] .init.text        PROGBITS 0000000000000120 000160 000020 00 WAX  0 0 16
  [ 3] .rodata           PROGBITS 0000000000000140 000180 000030 00   A  0 0 8
"""
    monkeypatch.setattr(
        VERIFY.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=output, stderr=""
        ),
    )
    assert VERIFY.executable_sections("llvm-readelf-21", tmp_path / "vmlinux") == [
        (".text", 0x120),
        (".init.text", 0x20),
    ]


def test_executable_section_parser_requires_text(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VERIFY.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="  [ 1] .init.text PROGBITS 0 40 20 00 AX 0 0 16\n",
            stderr="",
        ),
    )
    with pytest.raises(SystemExit, match="no usable executable-section inventory"):
        VERIFY.executable_sections("llvm-readelf-21", tmp_path / "vmlinux")
