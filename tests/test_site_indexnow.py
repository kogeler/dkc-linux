from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dkc.site import CONTENT_TYPES, SiteError, SiteManifest, SiteObject
from dkc.site_indexnow import (
    PUBLICATION_SCHEMA,
    changed_pages,
    load_publication_summary,
    submission,
)

SITE_URL = "https://site.example.net/"
REVISION = "0123456789abcdef0123456789abcdef01234567"
KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


def _manifest(*keys: str) -> SiteManifest:
    objects = tuple(
        SiteObject(
            key=key,
            sha256=hashlib.sha256(key.encode()).hexdigest(),
            size=len(key),
            content_type=CONTENT_TYPES[Path(key).suffix],
        )
        for key in sorted(keys)
    )
    return SiteManifest(SITE_URL, REVISION, objects)


def _summary(pages: list[str], **changes: object) -> dict[str, object]:
    return {
        "changed_pages": pages,
        "schema": PUBLICATION_SCHEMA,
        "source_revision": REVISION,
        "status": "PASS",
        **changes,
    }


def test_changed_pages_map_to_public_urls() -> None:
    manifest = _manifest("index.html", "docs/A.html", "assets/x.css")
    changed = changed_pages(
        _summary(["docs/A.html", "docs/gone.html", "index.html"]), manifest
    )
    assert changed.present == (f"{SITE_URL}docs/A.html", SITE_URL)
    assert changed.removed == (f"{SITE_URL}docs/gone.html",)


@pytest.mark.parametrize(
    ("summary", "message"),
    [
        (_summary([], status="FAIL"), "successful"),
        (_summary([], source_revision="f" * 40), "another site build"),
        (_summary(["index.html", "docs/A.html"]), "sorted"),
        (_summary(["assets/x.css"]), "not an HTML page"),
        (_summary(["../index.html"]), "unsafe"),
        (_summary([], changed_pages=None), "changed pages"),
    ],
)
def test_changed_pages_reject_foreign_or_malformed_summaries(
    summary: dict[str, object], message: str
) -> None:
    with pytest.raises(SiteError, match=message):
        changed_pages(summary, _manifest("index.html"))


def test_the_summary_must_match_its_evidence(tmp_path: Path) -> None:
    body = json.dumps(_summary(["index.html"])).encode()
    (tmp_path / "summary.json").write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    (tmp_path / "evidence.sha256").write_text(f"{digest}  summary.json\n")
    assert load_publication_summary(tmp_path)["changed_pages"] == ["index.html"]
    (tmp_path / "summary.json").write_bytes(body.replace(b"PASS", b"FAIL"))
    with pytest.raises(SiteError, match="evidence"):
        load_publication_summary(tmp_path)


def test_a_submission_names_the_key_file_and_stays_on_the_site() -> None:
    assert submission(SITE_URL, KEY, [SITE_URL]) == {
        "host": "site.example.net",
        "key": KEY,
        "keyLocation": f"{SITE_URL}{KEY}.txt",
        "urlList": [SITE_URL],
    }
    with pytest.raises(SiteError, match="outside"):
        submission(SITE_URL, KEY, ["https://other.example.net/"])
    with pytest.raises(SiteError, match="1 to 10,000"):
        submission(SITE_URL, KEY, [])
