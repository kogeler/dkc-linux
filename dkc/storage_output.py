"""Fail-closed output sanitizing for secret-bearing storage commands."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import re
import stat
import sys
import traceback
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = ["StorageRedactor", "run_with_sanitized_output"]


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _encoded_variants(value: str) -> set[str]:
    encoded = value.encode("utf-8")
    variants = {
        value,
        urllib.parse.quote(value, safe=""),
        urllib.parse.quote_plus(value, safe=""),
        json.dumps(value, ensure_ascii=True)[1:-1],
        base64.b64encode(encoded).decode("ascii"),
        base64.urlsafe_b64encode(encoded).decode("ascii"),
        encoded.hex(),
    }
    parsed = urllib.parse.urlsplit(value)
    if parsed.hostname:
        variants.add(parsed.hostname)
        variants.add(parsed.netloc)
        variants.update(segment for segment in parsed.path.split("/") if segment)
    return {item for item in variants if item}


@dataclass(frozen=True, repr=False)
class StorageRedactor:
    """All connection-file string values in directly printable forms."""

    markers: tuple[str, ...]

    @classmethod
    def from_path(
        cls, path: Path, *, additional_values: Iterable[str] = ()
    ) -> StorageRedactor:
        resolved = path.resolve(strict=True)
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
            raise ValueError("connection input is not a bounded regular file")
        raw = resolved.read_text(encoding="utf-8")
        values: list[str] = []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # A malformed file must still not leak quoted values through a
            # parser error or an unexpected traceback.
            for token in re.findall(r'"(?:[^"\\]|\\.)*"', raw):
                try:
                    decoded = json.loads(token)
                except json.JSONDecodeError:
                    continue
                if isinstance(decoded, str):
                    values.append(decoded)
        else:
            values.extend(_strings(parsed))
        markers: set[str] = set()
        for value in (*values, *additional_values):
            markers.update(_encoded_variants(value))
        return cls(tuple(sorted(markers, key=len, reverse=True)))

    def redact(self, value: str) -> str:
        result = value
        replacement = next(
            (
                candidate
                for candidate in ("[redacted]", "[filtered]", "[removed]")
                if not any(marker in candidate for marker in self.markers)
            ),
            "",
        )
        for marker in self.markers:
            result = result.replace(marker, replacement)
        result = result.replace("\r", " ")
        result = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "?", result)
        # A remote error body must not be able to emit a GitHub workflow
        # command after it passes through this boundary.
        return re.sub(r"(?m)^::", " : :", result)

    def contains_connection_value(self, value: str) -> bool:
        return any(marker in value for marker in self.markers)


class _BoundedCapture(io.StringIO):
    def __init__(self, maximum: int) -> None:
        super().__init__()
        self.maximum = maximum
        self.length = 0
        self.truncated = False

    def write(self, value: str) -> int:
        if not isinstance(value, str):
            raise TypeError("sanitized output accepts text only")
        remaining = self.maximum - self.length
        if remaining > 0:
            kept = value[:remaining]
            super().write(kept)
            self.length += len(kept)
        if len(value) > max(remaining, 0):
            self.truncated = True
        return len(value)

    def value(self) -> str:
        result = self.getvalue()
        if self.truncated:
            result += "\n[storage command output truncated]\n"
        return result


def run_with_sanitized_output(
    redactor: StorageRedactor,
    callback: Callable[[], int],
    *,
    maximum_stream_chars: int = 1_048_576,
) -> int:
    """Release no command output until every connection value is removed.

    Capturing first rather than replacing individual ``print`` calls also
    covers traceback formatting and writes split across multiple calls. The
    storage commands execute no child process after credentials are attached;
    low-level container-runtime diagnostics therefore remain outside this
    secret-bearing boundary.
    """

    if maximum_stream_chars < 4096:
        raise ValueError("sanitized output bound is too small")
    stdout_capture = _BoundedCapture(maximum_stream_chars)
    stderr_capture = _BoundedCapture(maximum_stream_chars)
    status = 1
    with contextlib.redirect_stdout(stdout_capture), contextlib.redirect_stderr(
        stderr_capture
    ):
        try:
            status = callback()
        except BaseException:
            traceback.print_exc()
            status = 1

    sanitized_stdout = redactor.redact(stdout_capture.value())
    sanitized_stderr = redactor.redact(stderr_capture.value())
    if redactor.contains_connection_value(sanitized_stdout + sanitized_stderr):
        sys.stderr.write("FAIL storage output sanitizer invariant\n")
        return 1
    sys.stdout.write(sanitized_stdout)
    sys.stderr.write(sanitized_stderr)
    sys.stdout.flush()
    sys.stderr.flush()
    return status
