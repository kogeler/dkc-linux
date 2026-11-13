"""Source members are downloaded with bounded retries and never left partial."""

from __future__ import annotations

import pathlib
import threading
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from dkc.cli import download

BODY = bytes(range(256)) * (4 * 4096 + 3)


@dataclass
class Server:
    url: str
    failures: int = 0
    agents: list[str] = field(default_factory=list)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setattr(download.time, "sleep", lambda seconds: None)
    state = Server(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            state.agents.append(self.headers.get("User-Agent", ""))
            if state.failures:
                state.failures -= 1
                self.send_error(503)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            self.wfile.write(BODY)

        def log_message(self, format: str, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_address[1]}/linux.orig.tar.xz"
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_transient_failures_are_retried_and_the_whole_body_arrives(
    server: Server, tmp_path: pathlib.Path
) -> None:
    server.failures = download.ATTEMPTS - 1
    target = tmp_path / "linux.orig.tar.xz"

    download.fetch(server.url, target, "dkc-test/1")

    assert target.read_bytes() == BODY
    assert server.agents == ["dkc-test/1"] * download.ATTEMPTS
    assert sorted(path.name for path in tmp_path.iterdir()) == [target.name]


def test_persistent_failure_is_bounded_and_leaves_no_file(
    server: Server, tmp_path: pathlib.Path
) -> None:
    server.failures = download.ATTEMPTS + 1
    target = tmp_path / "linux.orig.tar.xz"

    with pytest.raises(urllib.error.HTTPError):
        download.fetch(server.url, target, "dkc-test/1")

    assert len(server.agents) == download.ATTEMPTS
    assert not any(tmp_path.iterdir())
