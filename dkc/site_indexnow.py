"""IndexNow notification for the pages one site publication changed.

IndexNow lets a site tell participating search engines which URLs changed. The
site build publishes the key as `<key>.txt` at the site root; a notification
names that file as its key location. Only pages the publication actually wrote
or removed are submitted.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .serialize import loads
from .site import SiteError, SiteManifest, page_url, validate_key

__all__ = [
    "INDEXNOW_ENDPOINT",
    "MAX_URLS",
    "PUBLICATION_SCHEMA",
    "ChangedPages",
    "changed_pages",
    "describe_response",
    "load_publication_summary",
    "submission",
]

INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
PUBLICATION_SCHEMA = "dkc.site-publication-result.v1"

# IndexNow accepts at most this many URLs in one request.
MAX_URLS = 10_000

_RESPONSES = {
    200: "accepted",
    202: "accepted; the key is still being validated",
    400: "bad request: the payload is malformed",
    403: "forbidden: the key file was not found or does not hold the key",
    422: "unprocessable: a URL does not belong to the host or the key location",
    429: "too many requests: submit less often",
}
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ChangedPages:
    """Public URLs of pages one publication wrote, and of pages it removed."""

    present: tuple[str, ...]
    removed: tuple[str, ...]


def load_publication_summary(result: Path) -> dict[str, object]:
    """Read one site publication summary after checking its evidence record."""

    evidence = (result / "evidence.sha256").read_text(encoding="utf-8").splitlines()
    digests: dict[str, str] = {}
    for line in evidence:
        digest, separator, name = line.partition("  ")
        if not separator or not _SHA256_RE.fullmatch(digest) or name in digests:
            raise SiteError("site publication evidence is malformed")
        digests[name] = digest
    body = (result / "summary.json").read_bytes()
    if digests.get("summary.json") != hashlib.sha256(body).hexdigest():
        raise SiteError("site publication summary does not match its evidence")
    summary = loads(body)
    if not isinstance(summary, dict):
        raise SiteError("site publication summary is not an object")
    return summary


def changed_pages(summary: dict[str, object], manifest: SiteManifest) -> ChangedPages:
    """Map the pages a successful publication of this build changed to URLs."""

    if summary.get("schema") != PUBLICATION_SCHEMA or summary.get("status") != "PASS":
        raise SiteError("site publication summary is not a successful publication")
    if summary.get("source_revision") != manifest.source_revision:
        raise SiteError("site publication summary belongs to another site build")
    keys = summary.get("changed_pages")
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise SiteError("site publication summary lacks its changed pages")
    if keys != sorted(set(keys)):
        raise SiteError("changed pages must be unique and sorted")
    built = {item.key for item in manifest.objects}
    present: list[str] = []
    removed: list[str] = []
    for key in keys:
        if not validate_key(key).endswith(".html"):
            raise SiteError(f"changed page is not an HTML page: {key}")
        url = page_url(manifest.site_url, key)
        (present if key in built else removed).append(url)
    return ChangedPages(tuple(present), tuple(removed))


def submission(site_url: str, key: str, urls: list[str]) -> dict[str, object]:
    """Build one IndexNow request body for URLs below the site root."""

    if not urls or len(urls) > MAX_URLS:
        raise SiteError("an IndexNow submission needs 1 to 10,000 URLs")
    if any(not url.startswith(site_url) for url in urls):
        raise SiteError("an IndexNow URL is outside the site")
    return {
        "host": urlsplit(site_url).hostname,
        "key": key,
        "keyLocation": f"{site_url}{key}.txt",
        "urlList": urls,
    }


def describe_response(status: int) -> str:
    return _RESPONSES.get(status, "unexpected response")
