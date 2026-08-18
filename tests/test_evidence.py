from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from dkc.evidence import verify_evidence_directory


def _write_handoff(root: Path, files: dict[str, bytes]) -> None:
    root.mkdir()
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (root / "evidence.sha256").write_text(
        "".join(
            f"{hashlib.sha256(body).hexdigest()}  {name}\n"
            for name, body in sorted(files.items())
        ),
        encoding="utf-8",
    )


def test_exact_evidence_handoff_accepts_only_its_inventory(tmp_path: Path) -> None:
    root = tmp_path / "handoff"
    _write_handoff(root, {"nested/value.json": b"{}\n", "result.env": b"status=PASS\n"})
    assert verify_evidence_directory(root) == ("nested/value.json", "result.env")

    (root / "unlisted").write_bytes(b"addition")
    with pytest.raises(ValueError, match="does not match"):
        verify_evidence_directory(root)


def test_exact_evidence_handoff_rejects_mutation_and_links(tmp_path: Path) -> None:
    root = tmp_path / "handoff"
    _write_handoff(root, {"value": b"original"})
    (root / "value").write_bytes(b"changed")
    with pytest.raises(ValueError, match="verification failed"):
        verify_evidence_directory(root)

    (root / "value").unlink()
    (root / "value").symlink_to(root / "evidence.sha256")
    with pytest.raises(ValueError, match="symbolic link"):
        verify_evidence_directory(root)


@pytest.mark.parametrize("unsafe", ("../value", "/value", "./value", "nested//value"))
def test_exact_evidence_handoff_rejects_unsafe_paths(
    tmp_path: Path, unsafe: str
) -> None:
    root = tmp_path / "handoff"
    root.mkdir()
    (root / "evidence.sha256").write_text(
        f"{'0' * 64}  {unsafe}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unsafe path"):
        verify_evidence_directory(root)
