"""Notify IndexNow search engines about the pages one publication changed.

Run after a successful site publication. The command waits until the public
site serves the key file and the published bytes of every changed page, then
submits those URLs once. A page whose edge copy is still older when the wait
ends is left to ordinary sitemap crawling. Removed pages are submitted so
search engines learn they are gone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

from dkc.site import (
    INDEXNOW_KEY_FILE,
    MAX_OBJECT_BYTES,
    SiteError,
    page_url,
    validate_indexnow_key,
)
from dkc.site_indexnow import (
    INDEXNOW_ENDPOINT,
    changed_pages,
    describe_response,
    load_publication_summary,
    submission,
)
from dkc.site_publication import load_site_build

# The delivery layer rejects some generic client user agents.
USER_AGENT = "dkc-site-indexnow/1"
POLL_SECONDS = 15
RESPONSE_LIMIT = 64 * 1024


def fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read(MAX_OBJECT_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except OSError:
        return 0, b""


def post(endpoint: str, body: dict[str, object]) -> tuple[int, bytes]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(RESPONSE_LIMIT)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(RESPONSE_LIMIT)


def main(args: argparse.Namespace) -> int:
    build = load_site_build(args.site_result)
    manifest = build.manifest
    site_url = manifest.site_url
    key = validate_indexnow_key(
        (args.repository / INDEXNOW_KEY_FILE).read_text(encoding="ascii").strip()
    )
    if build.bodies.get(f"{key}.txt") != key.encode("ascii"):
        raise SiteError("the site build does not publish this IndexNow key")
    changed = changed_pages(load_publication_summary(args.publication_result), manifest)
    if not changed.present and not changed.removed:
        print("IndexNow: the publication changed no page; nothing to submit")
        return 0

    digests = {
        page_url(site_url, item.key): item.sha256
        for item in manifest.objects
        if item.key.endswith(".html")
    }
    key_url = f"{site_url}{key}.txt"
    key_served = False
    pending = set(changed.present)
    served: set[str] = set()
    deadline = time.monotonic() + args.wait_seconds
    while True:
        if not key_served:
            status, body = fetch(key_url)
            key_served = status == 200 and body == key.encode("ascii")
        for url in sorted(pending):
            status, body = fetch(url)
            if status == 200 and hashlib.sha256(body).hexdigest() == digests[url]:
                served.add(url)
                pending.discard(url)
        if (key_served and not pending) or time.monotonic() + POLL_SECONDS > deadline:
            break
        time.sleep(POLL_SECONDS)

    if not key_served:
        print(f"FAIL IndexNow: {key_url} does not serve the key", file=sys.stderr)
        return 1
    for url in sorted(pending):
        print(f"IndexNow: skipping {url}: the edge still serves older bytes")
    urls = sorted(served) + list(changed.removed)
    if not urls:
        print("IndexNow: no changed page serves its new bytes yet; nothing submitted")
        return 0
    try:
        status, body = post(args.endpoint, submission(site_url, key, urls))
    except OSError as exc:
        print(f"FAIL IndexNow: request failed: {exc}", file=sys.stderr)
        return 1
    print(f"IndexNow: HTTP {status} ({describe_response(status)}) for:")
    for url in urls:
        print(f"  {url}")
    text = body.decode("utf-8", "replace").strip()
    if text:
        print(f"IndexNow response body: {text[:2000]}")
    return 0 if status in (200, 202) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=pathlib.Path, required=True)
    parser.add_argument("--site-result", type=pathlib.Path, required=True)
    parser.add_argument("--publication-result", type=pathlib.Path, required=True)
    parser.add_argument("--wait-seconds", type=int, required=True)
    parser.add_argument("--endpoint", default=INDEXNOW_ENDPOINT)
    args = parser.parse_args()
    if args.wait_seconds < 0:
        parser.error("--wait-seconds must not be negative")
    if not args.endpoint.startswith("https://"):
        parser.error("--endpoint must be an HTTPS URL")
    return args


if __name__ == "__main__":
    parsed = parse_args()
    try:
        raise SystemExit(main(parsed))
    except (OSError, SiteError) as exc:
        print(f"FAIL IndexNow: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
