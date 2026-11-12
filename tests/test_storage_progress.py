from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from dkc.storage_output import StorageRedactor, run_with_sanitized_output
from dkc.storage_progress import StoragePhase, StorageProgress, StorageProgressEvent


def test_progress_is_live_while_arbitrary_output_remains_buffered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    connection = tmp_path / "connection.json"
    connection.write_text(json.dumps({"secret": "progress-canary-secret"}))
    redactor = StorageRedactor.from_path(connection)
    progress = StorageProgress(redactor, sys.stdout)

    def callback() -> int:
        sys.stderr.write("progress-canary-")
        progress(StorageProgressEvent(StoragePhase.IMMUTABLE, total_objects=3))
        captured = capsys.readouterr()
        assert "phase=prepare-immutable-objects" in captured.out
        assert captured.err == ""
        sys.stderr.write("secret\n")
        return 0

    assert run_with_sanitized_output(redactor, callback) == 0
    captured = capsys.readouterr()
    assert "progress-canary-secret" not in captured.err
    assert "[redacted]" in captured.err


def test_progress_throttles_updates_and_records_phase_duration() -> None:
    now = [0.0]
    stream = io.StringIO()
    progress = StorageProgress(StorageRedactor(()), stream, clock=lambda: now[0])
    progress(
        StorageProgressEvent(StoragePhase.IMMUTABLE, total_objects=4, total_bytes=40)
    )
    now[0] = 1
    progress(StorageProgressEvent(StoragePhase.IMMUTABLE, 1, 4, 10, 40))
    assert len(stream.getvalue().splitlines()) == 1
    now[0] = 16
    progress(StorageProgressEvent(StoragePhase.IMMUTABLE, 2, 4, 20, 40))
    assert len(stream.getvalue().splitlines()) == 2
    now[0] = 17
    progress(StorageProgressEvent(StoragePhase.IMMUTABLE, 4, 4, 40, 40))
    progress(StorageProgressEvent(StoragePhase.VERIFY_REPOSITORY))
    assert len(stream.getvalue().splitlines()) == 4
    assert progress.summary()[0] == {
        "phase": "prepare-immutable-objects",
        "elapsed_seconds": 17,
        "objects": 4,
        "total_objects": 4,
        "bytes": 40,
        "total_bytes": 40,
    }


def test_progress_redacts_even_a_connection_value_matching_a_phase() -> None:
    stream = io.StringIO()
    progress = StorageProgress(StorageRedactor(("immutable",)), stream)
    progress(StorageProgressEvent(StoragePhase.IMMUTABLE))
    assert "immutable" not in stream.getvalue()
    assert "[redacted]" in stream.getvalue()


@pytest.mark.parametrize("phase", ["::error::injected", "private-host.invalid"])
def test_progress_rejects_arbitrary_text(phase: str) -> None:
    with pytest.raises(ValueError, match="fixed phases"):
        StorageProgressEvent(phase)  # type: ignore[arg-type]


@pytest.mark.parametrize("counter", ["private-key", True, -1, 1.5])
def test_progress_rejects_non_numeric_counters(counter: object) -> None:
    with pytest.raises(ValueError, match="counters"):
        StorageProgressEvent(StoragePhase.IMMUTABLE, counter)  # type: ignore[arg-type]
