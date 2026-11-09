"""Local HTTPS S3 contract fixture, with real sockets and request accounting."""

from __future__ import annotations

import hashlib
import socket
import ssl
import subprocess
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dkc.s3 import S3Client, S3Credentials, S3Endpoint
from dkc.storage import ObjectMetadata
from tests.fake_storage import ConditionalObjectStore, PreconditionFailed


def certificate(root: Path) -> tuple[Path, Path]:
    cert = root / "cert.pem"
    key = root / "key.pem"
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1",
        "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1",
        "-addext", "basicConstraints=critical,CA:TRUE",
    ], check=True, capture_output=True)
    return cert, key


class HttpStorage:
    def __init__(self, cert: Path, key: Path) -> None:
        self.store = ConditionalObjectStore()
        self.requests: Counter[tuple[str, str]] = Counter()
        self.received_bytes: Counter[str] = Counter()
        self.returned_bytes: Counter[str] = Counter()
        self.drop_reply_for: str | None = None
        self.transient_head_for: str | None = None
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def handle_request(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/fixture" and self.command == "GET":
                    query = urllib.parse.parse_qs(parsed.query)
                    keys = fixture.store.list_keys(query.get("prefix", [""])[0])
                    start = int(query.get("continuation-token", ["0"])[0])
                    limit = int(query.get("max-keys", ["1000"])[0])
                    page = keys[start:start + limit]
                    root = ET.Element("ListBucketResult")
                    more = start + len(page) < len(keys)
                    ET.SubElement(root, "IsTruncated").text = "true" if more else "false"
                    if more:
                        ET.SubElement(root, "NextContinuationToken").text = str(start + len(page))
                    for item in page:
                        value = fixture.store.get(item)
                        assert value is not None
                        content = ET.SubElement(root, "Contents")
                        ET.SubElement(content, "Key").text = item
                        ET.SubElement(content, "Size").text = str(len(value.body))
                    body = ET.tostring(root)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                logical = urllib.parse.unquote(parsed.path).removeprefix("/fixture/")
                fixture.requests[self.command, logical] += 1
                value = fixture.store.get(logical)
                if self.command in ("GET", "HEAD"):
                    if self.command == "HEAD" and fixture.transient_head_for == logical:
                        fixture.transient_head_for = None
                        self.send_error(503)
                        return
                    if value is None:
                        self.send_error(404)
                        return
                    if self.headers.get("If-Match", value.etag) != value.etag:
                        self.send_error(412)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(value.body)))
                    self.send_header("ETag", value.etag)
                    self.send_header("Content-Type", value.metadata.content_type)
                    self.send_header("Cache-Control", value.metadata.cache_control)
                    self.end_headers()
                    if self.command == "GET":
                        fixture.returned_bytes[logical] += len(value.body)
                        self.wfile.write(value.body)
                    return
                if self.command == "PUT":
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                    fixture.received_bytes[logical] += len(body)
                    if self.headers["x-amz-content-sha256"] != hashlib.sha256(body).hexdigest():
                        self.send_error(400)
                        return
                    try:
                        written = fixture.store.put(
                            logical, body,
                            ObjectMetadata(self.headers["Content-Type"], self.headers["Cache-Control"]),
                            if_none_match=self.headers.get("If-None-Match") == "*",
                            if_match=self.headers.get("If-Match"),
                        )
                    except PreconditionFailed:
                        self.send_error(412)
                        return
                    if fixture.drop_reply_for == logical:
                        fixture.drop_reply_for = None
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.send_response(200)
                    self.send_header("ETag", written.etag)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.command == "DELETE":
                    fixture.store.delete(logical)
                    self.send_response(204)
                    self.end_headers()
                    return
                self.send_error(405)

            do_GET = handle_request
            do_HEAD = handle_request
            do_PUT = handle_request
            do_DELETE = handle_request

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.client = S3Client(
            S3Endpoint.validated(
                f"https://127.0.0.1:{self.server.server_port}",
                "fixture", "test-region", "path",
            ),
            S3Credentials("fixture-access", "fixture-secret"),
            sleeper=lambda _: None,
        )

    def reset_counters(self) -> None:
        self.requests.clear()
        self.received_bytes.clear()
        self.returned_bytes.clear()

    def close(self) -> None:
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
