from __future__ import annotations

import hashlib
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dkc.s3 import (
    HttpResponse,
    PreconditionFailed,
    RemoteStoreError,
    S3Client,
    S3Credentials,
    S3Endpoint,
    exact_http_request,
)
from dkc.storage import ObjectMetadata


ENDPOINT = S3Endpoint.validated(
    "https://objects.example.net",
    "dkc-test",
    "test-region",
    "path",
)
CREDENTIALS = S3Credentials("access-key", "secret-key")
MUTABLE = ObjectMetadata("application/json", "public, max-age=0, must-revalidate")
NOW = datetime(2026, 8, 16, 12, 34, 56, tzinfo=timezone.utc)


def test_endpoint_rejects_noncanonical_and_insecure_urls() -> None:
    with pytest.raises(ValueError, match="exact HTTPS"):
        S3Endpoint.validated(
            "http://objects.example.net",
            "dkc-test",
            "test-region",
            "path",
        )
    with pytest.raises(ValueError, match="exact HTTPS"):
        S3Endpoint.validated(
            "https://objects.example.net/path",
            "dkc-test",
            "test-region",
            "path",
        )


def test_connection_objects_do_not_expose_values_in_repr() -> None:
    assert "objects.example.net" not in repr(ENDPOINT)
    assert "access-key" not in repr(CREDENTIALS)
    assert "secret-key" not in repr(CREDENTIALS)


def test_exact_http_requests_reject_redirects_without_forwarding_auth() -> None:
    reached = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            nonlocal reached
            if self.path == "/start":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
                self.end_headers()
                return
            reached += 1
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/start",
            headers={"Authorization": "Bearer must-not-be-forwarded"},
        )
        response = exact_http_request(request, 2)
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert response.status == 302
    assert reached == 0


def test_signature_matches_the_published_single_chunk_s3_vector() -> None:
    endpoint = S3Endpoint.validated(
        "https://s3.amazonaws.com", "examplebucket", "us-east-1", "virtual"
    )
    credentials = S3Credentials(
        "AKIAIOSFODNN7EXAMPLE",
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    )
    client = S3Client(endpoint, credentials, clock=lambda: datetime(2013, 5, 24, tzinfo=timezone.utc))
    request = client.signed_request(
        "GET", "test.txt", headers={"range": "bytes=0-9"}
    )
    assert request.url == "https://examplebucket.s3.amazonaws.com/test.txt"
    assert hashlib.sha256(request.canonical_request.encode()).hexdigest() == (
        "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972"
    )
    assert request.headers["authorization"].endswith(
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )


def test_literal_put_signs_payload_metadata_and_create_precondition() -> None:
    captured = []

    def transport(request):  # type: ignore[no-untyped-def]
        captured.append(request)
        return HttpResponse(200, {"ETag": '"opaque-1"'}, b"")

    client = S3Client(ENDPOINT, CREDENTIALS, transport=transport, clock=lambda: NOW)
    etag = client.put(
        "_dkc-test/storage/owner/repo/run-0123456789abcdef0123456789abcdef/x",
        b"payload",
        MUTABLE,
        if_none_match=True,
    )
    assert etag == '"opaque-1"'
    assert len(captured) == 1
    request = captured[0]
    assert request.method == "PUT"
    assert request.body == b"payload"
    assert request.headers["if-none-match"] == "*"
    assert request.headers["cache-control"] == MUTABLE.cache_control
    assert request.headers["content-type"] == MUTABLE.content_type
    assert "if-none-match:*\n" in request.canonical_request
    authorization = request.headers["authorization"]
    assert "SignedHeaders=" in authorization
    assert "if-none-match" in authorization
    assert "cache-control" in authorization
    assert "content-type" in authorization


def test_put_has_no_unconditional_or_mixed_precondition_mode() -> None:
    client = S3Client(ENDPOINT, CREDENTIALS, transport=lambda request: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError, match="exactly one"):
        client.put("safe/key", b"x", MUTABLE)
    with pytest.raises(ValueError, match="exactly one"):
        client.put(
            "safe/key", b"x", MUTABLE, if_none_match=True, if_match='"old"'
        )
    with pytest.raises(ValueError, match="opaque quoted"):
        client.put("safe/key", b"x", MUTABLE, if_match="unquoted")


def test_429_replays_the_same_stale_condition_and_body_then_surfaces_412() -> None:
    captured = []
    sleeps: list[float] = []

    def transport(request):  # type: ignore[no-untyped-def]
        captured.append(request)
        if len(captured) == 1:
            return HttpResponse(429, {"Retry-After": "1"}, b"throttled")
        return HttpResponse(412, {}, b"lost")

    client = S3Client(
        ENDPOINT,
        CREDENTIALS,
        transport=transport,
        clock=lambda: NOW,
        sleeper=sleeps.append,
    )
    with pytest.raises(PreconditionFailed):
        client.put("safe/key", b"same-body", MUTABLE, if_match='"stale"')
    assert len(captured) == 2
    assert sleeps == [1.0]
    assert {request.body for request in captured} == {b"same-body"}
    assert {request.headers["if-match"] for request in captured} == {'"stale"'}
    assert all("if-none-match" not in request.headers for request in captured)


def test_remote_error_body_is_never_reflected_into_an_exception() -> None:
    marker = "remote-reflected-connection-value"

    def transport(request):  # type: ignore[no-untyped-def]
        return HttpResponse(500, {}, marker.encode())

    client = S3Client(ENDPOINT, CREDENTIALS, transport=transport, clock=lambda: NOW)
    with pytest.raises(RemoteStoreError) as captured:
        client.get("safe/key")
    assert marker not in str(captured.value)
    assert str(captured.value) == "GetObject safe/key failed with HTTP 500"


def test_list_objects_v2_is_paginated_and_cannot_escape_prefix() -> None:
    requests = []

    def transport(request):  # type: ignore[no-untyped-def]
        requests.append(request)
        if "continuation-token=" not in request.url:
            body = b"""<ListBucketResult><IsTruncated>true</IsTruncated>
              <Contents><Key>safe/prefix/a</Key><Size>11</Size></Contents>
              <NextContinuationToken>next</NextContinuationToken></ListBucketResult>"""
        else:
            body = b"""<ListBucketResult><IsTruncated>false</IsTruncated>
              <Contents><Key>safe/prefix/b</Key><Size>12</Size></Contents></ListBucketResult>"""
        return HttpResponse(200, {}, body)

    client = S3Client(ENDPOINT, CREDENTIALS, transport=transport, clock=lambda: NOW)
    assert client.list_keys("safe/prefix/", page_size=1) == (
        "safe/prefix/a",
        "safe/prefix/b",
    )
    assert [(item.key, item.size) for item in client.list_objects("safe/prefix/")] == [
        ("safe/prefix/a", 11),
        ("safe/prefix/b", 12),
    ]
    assert "max-keys=1" in requests[0].url
    assert "continuation-token=next" in requests[1].url

    with pytest.raises(ValueError, match="page size"):
        client.list_keys("safe/prefix/", page_size=1001)

    def escaped(request):  # type: ignore[no-untyped-def]
        return HttpResponse(
            200,
            {},
            b"<ListBucketResult><IsTruncated>false</IsTruncated>"
            b"<Contents><Key>production/object</Key><Size>1</Size></Contents>"
            b"</ListBucketResult>",
        )

    other = S3Client(ENDPOINT, CREDENTIALS, transport=escaped, clock=lambda: NOW)
    with pytest.raises(Exception, match="escaped requested prefix"):
        other.list_keys("safe/prefix/")


def test_empty_prefix_supports_an_authoritative_whole_bucket_inventory() -> None:
    def transport(request):  # type: ignore[no-untyped-def]
        assert "prefix=" in request.url
        return HttpResponse(
            200,
            {},
            b"<ListBucketResult><IsTruncated>false</IsTruncated>"
            b"<Contents><Key>pool/object</Key><Size>7</Size></Contents>"
            b"</ListBucketResult>",
        )

    client = S3Client(ENDPOINT, CREDENTIALS, transport=transport, clock=lambda: NOW)
    assert client.list_keys("") == ("pool/object",)


def test_list_objects_rejects_missing_or_invalid_size() -> None:
    def transport(request):  # type: ignore[no-untyped-def]
        del request
        return HttpResponse(
            200,
            {},
            b"<ListBucketResult><IsTruncated>false</IsTruncated>"
            b"<Contents><Key>pool/object</Key><Size>-1</Size></Contents>"
            b"</ListBucketResult>",
        )

    client = S3Client(ENDPOINT, CREDENTIALS, transport=transport, clock=lambda: NOW)
    with pytest.raises(RemoteStoreError, match="invalid object size"):
        client.list_objects("")
