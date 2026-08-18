from __future__ import annotations

import json
import sys
import traceback
import urllib.parse
from pathlib import Path

import pytest

from dkc.storage_connection import (
    MissingStorageConnection,
    load_connection,
    materialize_connection,
)
from dkc.storage_output import StorageRedactor, run_with_sanitized_output


def _connection() -> dict[str, str]:
    return {
        "s3_access_key_id": "local-access-key",
        "s3_addressing_style": "path",
        "s3_bucket": "empty-test-bucket",
        "s3_endpoint": "https://account.example.invalid",
        "s3_region": "auto",
        "s3_secret_access_key": "local-secret-key",
    }


def _write(path: Path, value: object, mode: int = 0o600) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)


def test_connection_file_is_exact_private_and_repr_safe(tmp_path: Path) -> None:
    path = tmp_path / "storage.json"
    _write(path, _connection())
    connection = load_connection(path)
    rendered = repr(connection)
    assert "local-access-key" not in rendered
    assert "local-secret-key" not in rendered
    assert "account.example.invalid" not in rendered


def test_connection_file_rejects_group_access_and_extra_fields(tmp_path: Path) -> None:
    path = tmp_path / "storage.json"
    _write(path, _connection(), 0o640)
    with pytest.raises(ValueError, match="group or other"):
        load_connection(path)

    _write(path, {**_connection(), "unexpected": "value"})
    with pytest.raises(ValueError, match="boundary mismatch"):
        load_connection(path)


def test_empty_required_connection_field_blocks(tmp_path: Path) -> None:
    path = tmp_path / "storage.json"
    _write(path, {**_connection(), "s3_secret_access_key": ""})
    with pytest.raises(MissingStorageConnection, match="s3_secret_access_key"):
        load_connection(path)


def test_every_connection_value_and_encoded_endpoint_are_redacted(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage.json"
    value = {
        "s3_access_key_id": "CANARYACCESS123456789",
        "s3_addressing_style": "virtual-canary-style",
        "s3_bucket": "canary-bucket-987654321",
        "s3_endpoint": "https://canary-account-123456789.example.invalid",
        "s3_region": "canary-region-123456789",
        "s3_secret_access_key": "CANARY_SECRET_123456789+/=",
        "s3_session_token": "CANARY_SESSION_123456789+/=",
    }
    _write(path, value)
    redactor = StorageRedactor.from_path(path)
    raw_markers = tuple(value.values())
    hostile = " ".join(raw_markers) + " " + urllib.parse.quote(
        value["s3_endpoint"], safe=""
    )
    rendered = redactor.redact(hostile)
    assert not any(marker in rendered for marker in raw_markers)
    assert urllib.parse.quote(value["s3_endpoint"], safe="") not in rendered


def test_sanitized_output_covers_split_writes_and_unexpected_tracebacks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "storage.json"
    value = _connection()
    value["s3_access_key_id"] = "trace-access-canary"
    value["s3_secret_access_key"] = "trace-secret-canary"
    value["s3_endpoint"] = "https://trace-endpoint-canary.example.invalid"
    value["s3_region"] = "trace-region-canary"
    value["s3_bucket"] = "trace-bucket-canary"
    _write(path, value)
    redactor = StorageRedactor.from_path(path)

    def callback() -> int:
        secret = value["s3_secret_access_key"]
        sys.stderr.write(secret[:8])
        sys.stderr.write(secret[8:] + "\n")
        print(value["s3_endpoint"])
        try:
            raise RuntimeError(" ".join(value.values()))
        except RuntimeError:
            traceback.print_exc()
        return 7

    assert run_with_sanitized_output(redactor, callback) == 7
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "Traceback" in combined
    assert not any(marker in combined for marker in value.values())
    assert "[redacted]" in combined


def test_sanitized_output_covers_common_encodings_and_an_additional_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "storage.json"
    value = _connection()
    value["s3_secret_access_key"] = "encoded-secret-canary"
    _write(path, value)
    token = "additional-token-canary"
    redactor = StorageRedactor.from_path(path, additional_values=(token,))

    import base64

    def callback() -> int:
        secret = value["s3_secret_access_key"].encode()
        print(base64.b64encode(secret).decode())
        print(secret.hex())
        print(token)
        print("::warning::remote-controlled annotation")
        print("control=\x1b[31m")
        return 0

    assert run_with_sanitized_output(redactor, callback) == 0
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert value["s3_secret_access_key"] not in combined
    assert base64.b64encode(value["s3_secret_access_key"].encode()).decode() not in combined
    assert value["s3_secret_access_key"].encode().hex() not in combined
    assert token not in combined
    assert "\n::warning::" not in combined
    assert "\x1b" not in combined


def test_malformed_connection_still_builds_a_fail_closed_redactor(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage.json"
    path.write_text('{"secret":"malformed-canary-value",', encoding="utf-8")
    path.chmod(0o600)
    redactor = StorageRedactor.from_path(path)
    assert "malformed-canary-value" not in redactor.redact(
        "parser failed near malformed-canary-value"
    )


def test_connection_materialization_is_private_validated_and_exclusive(
    tmp_path: Path,
) -> None:
    output = tmp_path / "stage" / "connection.json"
    environment = {
        "S3_ACCESS_KEY_ID": "materialized-access",
        "S3_ADDRESSING_STYLE": "path",
        "S3_BUCKET": "materialized-bucket",
        "S3_ENDPOINT": "https://materialized.example.invalid",
        "S3_REGION": "auto",
        "S3_SECRET_ACCESS_KEY": "materialized-secret",
        "S3_SESSION_TOKEN": "",
    }
    materialize_connection(output, environment=environment)
    assert output.stat().st_mode & 0o777 == 0o600
    assert load_connection(output).endpoint.bucket == "materialized-bucket"
    with pytest.raises(ValueError, match="already exists"):
        materialize_connection(output, environment=environment)


def test_connection_materialization_copies_and_rejects_unsafe_input(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.json"
    _write(source, _connection())
    output = tmp_path / "stage" / "connection.json"
    materialize_connection(output, provided=source)
    assert output.read_bytes() == source.read_bytes()

    unsafe = tmp_path / "unsafe.json"
    _write(unsafe, _connection(), 0o640)
    with pytest.raises(ValueError, match="group or other"):
        materialize_connection(tmp_path / "other.json", provided=unsafe)

    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(OSError):
        materialize_connection(tmp_path / "linked.json", provided=link)
