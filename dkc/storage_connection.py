"""Strict secret-file boundary for S3-compatible storage connections."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .s3 import S3Credentials, S3Endpoint
from .storage_output import StorageRedactor

__all__ = [
    "MissingStorageConnection",
    "StorageConnection",
    "load_connection",
    "materialize_connection",
]


_ENVIRONMENT_FIELDS = {
    "s3_access_key_id": "S3_ACCESS_KEY_ID",
    "s3_addressing_style": "S3_ADDRESSING_STYLE",
    "s3_bucket": "S3_BUCKET",
    "s3_endpoint": "S3_ENDPOINT",
    "s3_region": "S3_REGION",
    "s3_secret_access_key": "S3_SECRET_ACCESS_KEY",
    "s3_session_token": "S3_SESSION_TOKEN",
}


class MissingStorageConnection(RuntimeError):
    """Required connection fields are absent, so no request may begin."""


@dataclass(frozen=True, repr=False)
class StorageConnection:
    endpoint: S3Endpoint
    credentials: S3Credentials
    redactor: StorageRedactor

    def redact(self, value: str) -> str:
        return self.redactor.redact(value).replace("\n", " ")[:2048]


def _load_object(path: Path) -> dict[str, object]:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
        raise ValueError("connection input is not a bounded regular file")
    if info.st_mode & 0o077:
        raise ValueError("connection input must not grant group or other permissions")
    try:
        value = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("connection input is not valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("connection input must be one JSON object")
    return value


def load_connection(path: Path) -> StorageConnection:
    redactor = StorageRedactor.from_path(path)
    raw = _load_object(path)
    required = frozenset(
        {
            "s3_access_key_id",
            "s3_addressing_style",
            "s3_bucket",
            "s3_endpoint",
            "s3_region",
            "s3_secret_access_key",
        }
    )
    optional = frozenset({"s3_session_token"})
    if set(raw) != required | (set(raw) & optional):
        missing = sorted(required - set(raw))
        extra = sorted(set(raw) - required - optional)
        raise ValueError(
            f"connection field boundary mismatch: missing={missing} extra={extra}"
        )
    values: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ValueError(f"connection field {key} must be a string")
        values[key] = value
    missing_values = sorted(
        key for key in required if not values.get(key)
    )
    if missing_values:
        raise MissingStorageConnection(
            f"required connection fields are empty: {missing_values}"
        )

    endpoint = S3Endpoint.validated(
        values["s3_endpoint"],
        values["s3_bucket"],
        values["s3_region"],
        values["s3_addressing_style"],
    )
    credentials = S3Credentials(
        values["s3_access_key_id"],
        values["s3_secret_access_key"],
        values.get("s3_session_token"),
    )
    return StorageConnection(endpoint, credentials, redactor)


def materialize_connection(
    output: Path,
    *,
    provided: Path | None = None,
    environment: Mapping[str, str] | None = None,
    owner_uid: int | None = None,
) -> StorageConnection:
    """Create one private, validated connection file for a confined process.

    A provided file is copied after strict ownership and mode checks, closing
    the time-of-check/time-of-use window before the copy is mounted. Otherwise
    the exact supported fields are read from the process environment. The
    destination is exclusive, so a retry cannot silently replace credentials.
    """

    if output.exists() or output.is_symlink():
        raise ValueError("connection staging output already exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_environment = os.environ if environment is None else environment
    expected_owner = os.getuid() if owner_uid is None else owner_uid
    if provided is not None:
        source_descriptor = os.open(
            provided, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(source_descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("connection input must be a regular non-symlink file")
            if info.st_uid != expected_owner:
                raise ValueError("connection input must be owned by the current user")
            if info.st_mode & 0o077:
                raise ValueError(
                    "connection input must not grant group or other permissions"
                )
            if info.st_size > 65536:
                raise ValueError("connection input exceeds 65536 bytes")
            body = stream.read(65537)
        if len(body) != info.st_size or len(body) > 65536:
            raise ValueError("connection input changed or exceeds 65536 bytes")
    else:
        value = {
            field: source_environment.get(variable, "")
            for field, variable in _ENVIRONMENT_FIELDS.items()
            if field != "s3_session_token"
            or source_environment.get(variable, "")
        }
        body = (
            json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    created = False
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
        return load_connection(output)
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise
