from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dkc import schema
from dkc.site import (
    CONTENT_TYPES,
    MAX_KEY_LENGTH,
    MAX_OBJECTS,
    RESERVED_KEYS,
    RESERVED_PREFIXES,
    SITE_CACHE_CONTROL,
    SiteError,
    SiteManifest,
    SiteObject,
    SitePage,
    audit_site,
    discover_pages,
    format_fingerprint,
    inventory,
    link_home_to_root,
    manifest_document,
    nav_entries,
    page_description,
    parse_manifest,
    parse_page_dates,
    prune_output,
    render_llms,
    render_robots,
    render_sitemap,
    rewrite_links,
    stage_pages,
    validate_indexnow_key,
    validate_key,
)

ROOT = Path(__file__).resolve().parents[1]
REVISION = "0123456789abcdef0123456789abcdef01234567"
REPOSITORY = "https://github.com/example/dkc"
SITE_URL = "https://site.example.net/"
FINGERPRINT = "7B98D4BE13418D38BAC037D276349629CC453C26"
INDEXNOW_KEY = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rewrite(
    text: str, page: SitePage, pages: list[SitePage], existing: set[str]
) -> str:
    directories = {
        "/".join(path.split("/")[:index])
        for path in existing
        for index in range(1, path.count("/") + 1)
    }

    def kind(path: str) -> str | None:
        if path in existing:
            return "file"
        return "directory" if path in directories else None

    return rewrite_links(
        text,
        page=page,
        pages_by_source={item.source: item for item in pages},
        source_kind=kind,
        repository_url=REPOSITORY,
        revision=REVISION,
    )


def test_page_urls_follow_the_generator_layout() -> None:
    assert SitePage("README.md", "README.md").url_path == "index.html"
    assert SitePage("docs/README.md", "docs/README.md").url_path == "docs/index.html"
    assert SitePage("docs/BUILD.md", "docs/BUILD.md").url_path == "docs/BUILD.html"


def test_discovery_rejects_unmapped_hidden_pages(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# Home\n")
    _write(tmp_path / ".github/workflows/README.md", "# Workflows\n")
    _write(tmp_path / ".config/notes.md", "# Hidden\n")
    with pytest.raises(SiteError, match="hidden Markdown source"):
        discover_pages(tmp_path)


def test_links_are_rewritten_for_the_site_location() -> None:
    pages = [
        SitePage("README.md", "README.md"),
        SitePage("docs/A.md", "docs/A.md"),
        SitePage("docs/B.md", "docs/B.md"),
        SitePage(".github/workflows/README.md", "ci/README.md"),
    ]
    existing = {"README.md", "TODO.md", "keys/dkc-archive-keyring.gpg", "Makefile"}
    text = (
        "[home](../README.md#install) [peer](B.md) "
        "[ci](../.github/workflows/README.md)\n"
        "[todo](../TODO.md#pending) [keys](../keys/) [make](../Makefile)\n"
        "[web](https://example.org/x.md) [here](#local)\n"
        "Inline `[code](B.md)` and [`label`](B.md#part).\n"
        "```sh\n[fenced](B.md)\n```\n"
    )
    result = _rewrite(text, pages[1], pages, existing)
    assert "[home](../README.md#install)" in result
    assert "[peer](B.md)" in result
    assert "[ci](../ci/README.md)" in result
    assert f"[todo]({REPOSITORY}/blob/{REVISION}/TODO.md#pending)" in result
    assert f"[keys]({REPOSITORY}/tree/{REVISION}/keys)" in result
    assert f"[make]({REPOSITORY}/blob/{REVISION}/Makefile)" in result
    assert "[web](https://example.org/x.md) [here](#local)" in result
    assert "Inline `[code](B.md)` and [`label`](B.md#part)." in result
    assert "```sh\n[fenced](B.md)\n```" in result


def test_a_renamed_page_keeps_its_outgoing_links_valid() -> None:
    pages = [
        SitePage("docs/BUILD.md", "docs/BUILD.md"),
        SitePage(".github/workflows/README.md", "ci/README.md"),
    ]
    result = _rewrite("[build](../../docs/BUILD.md#x)\n", pages[1], pages, set())
    assert result == "[build](../docs/BUILD.md#x)\n"


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("missing.md", "does not exist"),
        ("../../outside.md", "escapes the repository"),
        ("/docs/A.md", "absolute link"),
    ],
)
def test_broken_or_unsafe_links_fail_closed(target: str, message: str) -> None:
    page = SitePage("docs/A.md", "docs/A.md")
    with pytest.raises(SiteError, match=message):
        _rewrite(f"[x]({target})\n", page, [page], set())


def test_staging_marks_only_the_home_page_and_rejects_front_matter(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write(source / "README.md", "# Home\n\nSee [docs](docs/A.md).\n")
    _write(
        source / "docs/A.md",
        '# A\n\nRead the [home](../README.md) page & "quotes" first.\n',
    )
    _write(source / "docs/B.md", "# B\n\nBack [home](../README.md).\n")
    _write(source / "AGENTS.md", "# Agents\n")
    _write(source / ".github/workflows/README.md", "# Workflows\n")
    destination = tmp_path / "pages"
    pages = stage_pages(
        source, destination, repository_url=REPOSITORY, revision=REVISION
    )
    assert [page.target for page in pages] == [
        "ci/README.md",
        "README.md",
        "docs/A.md",
        "docs/B.md",
    ]
    home = (destination / "README.md").read_text()
    assert home.startswith("---\nhide:\n  - navigation\n---\n")
    staged = (destination / "docs/A.md").read_text()
    assert staged.startswith(
        '---\ndescription: "Read the home page &amp; &quot;quotes&quot; first."\n'
        "---\n\n# A\n"
    )
    assert (destination / "docs/B.md").read_text().startswith("# B\n")
    assert not (destination / "AGENTS.md").exists()
    _write(source / "docs/A.md", "---\ntitle: x\n---\n# A\n")
    with pytest.raises(SiteError, match="front matter"):
        stage_pages(
            source, tmp_path / "again", repository_url=REPOSITORY, revision=REVISION
        )


def test_readme_keeps_the_structure_the_home_hero_styles() -> None:
    lines = (ROOT / "README.md").read_text(encoding="utf-8").split("\n\n")
    assert lines[0].startswith("# ") and "\n" not in lines[0]
    assert lines[1].startswith("**") and lines[1].endswith("**")
    links = re.findall(r"\[[^\]]+\]\([^)]+\)", lines[2])
    assert len(links) >= 2
    assert re.sub(r"\[[^\]]+\]\([^)]+\)", "", lines[2]).replace("·", "").split() == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "# T\n\n**Bold** lead with `code`, [a link](x.md), and *emphasis* here.\n",
            "Bold lead with code, a link, and emphasis here.",
        ),
        (
            "# T\n\n| a | b |\n| - | - |\n\n- list item one two three\n"
            "  continued item text here\n\nFirst real paragraph after the list.\n",
            "First real paragraph after the list.",
        ),
        ("# T\n\n```sh\nnot a lead at all here\n```\n\nShort.\n\n## Next\n", None),
        ("# T\n\n## Section\n\nText inside a later section only.\n", None),
        (
            "# T\n\nThis opening sentence is long enough to fill clearly more than "
            "half of the available space. " + "word " * 40 + "\n",
            "This opening sentence is long enough to fill clearly more than half of "
            "the available space.",
        ),
    ],
)
def test_page_description_uses_the_lead_paragraph(
    text: str, expected: str | None
) -> None:
    assert page_description(text) == expected


def test_page_description_is_shortened_at_a_word_boundary() -> None:
    lead = "Short opening. " + " ".join(f"word{index}" for index in range(60))
    description = page_description(f"# T\n\n{lead}\n")
    assert description is not None
    assert len(description) <= 160 and description.endswith("\u2026")
    assert description[:-1].split()[-1] in lead.split()


def test_navigation_flattens_sections_and_rejects_duplicates() -> None:
    nav = [
        {"Home": "README.md"},
        {"Guide": [{"A": "docs/A.md"}, {"Deeper": [{"B": "docs/B.md"}]}]},
        {"Source": "https://example.org/"},
    ]
    entries = nav_entries(nav)
    assert [(entry.section, entry.target) for entry in entries] == [
        (None, "README.md"),
        ("Guide", "docs/A.md"),
        ("Guide / Deeper", "docs/B.md"),
    ]
    with pytest.raises(SiteError, match="more than once"):
        nav_entries([{"A": "README.md"}, {"B": "README.md"}])
    with pytest.raises(SiteError):
        nav_entries([{"A": "README.md", "B": "docs/A.md"}])


def test_keys_are_safe_typed_and_never_shadow_the_repository() -> None:
    for key in ("index.html", "keys/index.html", "assets/stylesheets/dkc.css"):
        assert validate_key(key) == key
    for key in (
        "dists/trixie/index.html",
        "pool/main/x.js",
        "keys/dkc-archive-keyring.gpg",
        "../index.html",
        ".hidden.html",
        "docs/.x/a.html",
        "docs//a.html",
        "page.md",
        "binary.exe",
        "a" * (MAX_KEY_LENGTH - 4) + ".html",
    ):
        with pytest.raises(SiteError):
            validate_key(key)


def test_schema_and_code_agree_on_the_site_object_contract() -> None:
    document = schema.load("site-build")
    item = document["properties"]["objects"]["items"]["properties"]
    assert item["cache_control"]["const"] == SITE_CACHE_CONTROL
    assert set(item["content_type"]["enum"]) == set(CONTENT_TYPES.values())
    assert item["key"]["maxLength"] == MAX_KEY_LENGTH
    assert document["properties"]["objects"]["maxItems"] == MAX_OBJECTS
    suffixes = re.search(r"\\\.\(([a-z|]+)\)\$$", item["key"]["pattern"])
    assert suffixes is not None
    assert {f".{suffix}" for suffix in suffixes.group(1).split("|")} == set(
        CONTENT_TYPES
    )


def test_delivery_rules_match_the_site_key_contract() -> None:
    text = (ROOT / "docs/CLOUDFLARE_CACHE.md").read_text(encoding="utf-8")
    clause = text[text.index('starts_with(http.request.uri.path, "/site/") and') :]
    clause = clause[: clause.index("```")]
    assert f"le {len('/site/') + MAX_KEY_LENGTH}" in clause
    allowed = set(
        re.findall(r'ends_with\(http\.request\.uri\.path, "(\.[a-z]+)"\)', clause)
    )
    assert allowed == set(CONTENT_TYPES)
    rewrite = text[text.index("`dkc-linux site: map pages`") :]
    rewrite = rewrite[: rewrite.index("concat(")]
    excluded = set(re.findall(r'"(/keys/[^"]+)"', rewrite))
    assert {path.lstrip("/") for path in excluded} == RESERVED_KEYS
    for prefix in RESERVED_PREFIXES:
        assert f'not starts_with(http.request.uri.path, "/{prefix}")' in rewrite


def _object(key: str, body: bytes) -> SiteObject:
    import hashlib

    return SiteObject(
        key=key,
        sha256=hashlib.sha256(body).hexdigest(),
        size=len(body),
        content_type=CONTENT_TYPES[Path(key).suffix],
    )


def test_manifest_round_trips_and_rejects_unknown_fields() -> None:
    manifest = SiteManifest(
        SITE_URL,
        REVISION,
        (_object("docs/a.html", b"<p>a</p>"), _object("index.html", b"<p>home</p>")),
    )
    document = manifest_document(manifest)
    schema.validate("site-build", document)
    assert parse_manifest(json.loads(json.dumps(document))) == manifest
    with pytest.raises(SiteError):
        parse_manifest({**document, "extra": 1})
    broken = json.loads(json.dumps(document))
    broken["objects"][0]["cache_control"] = "public, max-age=60"
    with pytest.raises(SiteError):
        parse_manifest(broken)
    with pytest.raises(SiteError, match="sorted"):
        SiteManifest(SITE_URL, REVISION, tuple(reversed(manifest.objects)))


def _page(title: str, url: str, body: str) -> str:
    return (
        "<!doctype html><html><head>"
        f"<title>{title}</title>"
        f'<meta name="description" content="About {title}.">'
        f'<link rel="canonical" href="{url}">'
        '<link rel="stylesheet" href="ASSET/style.css">'
        "</head><body>"
        f'<h1 id="top">{title}</h1>{body}'
        "</body></html>"
    )


def _site(tmp_path: Path) -> tuple[Path, list[SitePage]]:
    site = tmp_path / "site"
    pages = [SitePage("README.md", "README.md"), SitePage("docs/A.md", "docs/A.md")]
    home = _page(
        "Home",
        SITE_URL,
        f"<p><code>{format_fingerprint(FINGERPRINT)}</code></p>"
        '<a href="docs/A.html#top">A</a>'
        f'<a href="{SITE_URL}keys/dkc-archive-keyring.gpg">key</a>'
        '<a href="https://github.com/example/dkc">source</a>',
    ).replace("ASSET", "assets")
    page = _page("A", f"{SITE_URL}docs/A.html", '<a href="../">home</a>')
    _write(site / "index.html", home)
    _write(site / "docs/A.html", page.replace("ASSET", "../assets"))
    _write(
        site / "assets/style.css",
        "body { background: url(data:image/png;base64,AA==); }\n",
    )
    _write(
        site / "sitemap.xml",
        render_sitemap(SITE_URL, pages, {"README.md": 1, "docs/A.md": 2}),
    )
    _write(site / "robots.txt", render_robots(SITE_URL))
    _write(site / f"{INDEXNOW_KEY}.txt", INDEXNOW_KEY)
    nav = nav_entries([{"Home": "README.md"}, {"Docs": [{"A": "docs/A.md"}]}])
    _write(
        site / "llms.txt",
        render_llms(
            site_name="DKC",
            description="Kernel packages.",
            site_url=SITE_URL,
            repository_url=REPOSITORY,
            nav=nav,
            pages=pages,
        ),
    )
    return site, pages


def _audit(site: Path, pages: list[SitePage]) -> object:
    return audit_site(
        site,
        site_url=SITE_URL,
        pages=pages,
        primary_fingerprint=FINGERPRINT,
        indexnow_key=INDEXNOW_KEY,
    )


def test_a_complete_site_passes_the_audit(tmp_path: Path) -> None:
    site, pages = _site(tmp_path)
    audit = _audit(site, pages)
    assert audit.pages == 2
    assert [item.key for item in audit.objects] == [
        f"{INDEXNOW_KEY}.txt",
        "assets/style.css",
        "docs/A.html",
        "index.html",
        "llms.txt",
        "robots.txt",
        "sitemap.xml",
    ]
    assert audit.objects == inventory(site)


@pytest.mark.parametrize(
    ("path", "old", "new", "message"),
    [
        (
            "index.html",
            "</head>",
            '<script src="https://cdn.example/x.js"></script></head>',
            "external",
        ),
        ("index.html", "docs/A.html#top", "docs/A.html#missing", "missing anchor"),
        ("index.html", "docs/A.html#top", "docs/A.html?h=x", "query"),
        ("index.html", "docs/A.html#top", "docs/", "directory reference"),
        ("index.html", "docs/A.html#top", "docs/B.html", "does not exist"),
        ("index.html", "7B98", "0000", "fingerprint"),
        (
            "index.html",
            '<h1 id="top">Home</h1>',
            "<h1>Home</h1><h1>Again</h1>",
            "one h1",
        ),
        ("docs/A.html", "docs/A.html", "docs/other.html", "canonical"),
        (
            "index.html",
            f'href="{SITE_URL}"',
            f'href="{SITE_URL}index.html"',
            "canonical",
        ),
        ("docs/A.html", 'href="../"', 'href="../index.html"', "site root"),
        (
            "docs/A.html",
            '<meta name="description" content="About A.">',
            "",
            "description",
        ),
        ("sitemap.xml", "+00:00</lastmod>", "</lastmod>", "lastmod"),
        (f"{INDEXNOW_KEY}.txt", INDEXNOW_KEY, "other", "IndexNow"),
        (
            "index.html",
            "</body>",
            '<div data-md-component="source"></div></body>',
            "third party",
        ),
        (
            "assets/style.css",
            "data:image/png;base64,AA==",
            "https://fonts.example/a.woff2",
            "external",
        ),
        ("robots.txt", "Allow: /", "Disallow: /", "robots"),
        ("sitemap.xml", "docs/A.html", "docs/B.html", "sitemap"),
    ],
)
def test_the_audit_rejects_each_contract_violation(
    tmp_path: Path, path: str, old: str, new: str, message: str
) -> None:
    site, pages = _site(tmp_path)
    target = site / path
    text = target.read_text(encoding="utf-8")
    assert old in text
    target.write_text(text.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(SiteError, match=message):
        _audit(site, pages)


_ANALYTICS = '<script id="__analytics">function __md_analytics(){}</script>'
_CONSENT_DIALOG = '<div data-md-component="consent" id="__consent" hidden></div>'
_CONSENT_GATE = (
    '<script>var consent=__md_get("__consent");'
    "consent&&consent.analytics&&__md_analytics()</script>"
)


@pytest.mark.parametrize(
    ("addition", "accepted"),
    [
        (_ANALYTICS + _CONSENT_DIALOG + _CONSENT_GATE, True),
        (_ANALYTICS + _CONSENT_GATE, False),
        (_ANALYTICS + _CONSENT_DIALOG + "<script>__md_analytics()</script>", False),
    ],
)
def test_analytics_must_wait_for_visitor_consent(
    tmp_path: Path, addition: str, accepted: bool
) -> None:
    site, pages = _site(tmp_path)
    target = site / "docs/A.html"
    text = target.read_text(encoding="utf-8")
    target.write_text(text.replace("</body>", addition + "</body>"), encoding="utf-8")
    if accepted:
        _audit(site, pages)
    else:
        with pytest.raises(SiteError, match="without visitor consent"):
            _audit(site, pages)


def test_the_audit_rejects_unexpected_and_forbidden_files(tmp_path: Path) -> None:
    site, pages = _site(tmp_path)
    _write(site / "extra.html", "<html></html>")
    with pytest.raises(SiteError, match="extra"):
        _audit(site, pages)
    (site / "extra.html").unlink()
    _write(site / "font.woff2", "x")
    with pytest.raises(SiteError, match="not allowed"):
        _audit(site, pages)


def test_pruning_removes_only_unserved_generator_output(tmp_path: Path) -> None:
    site = tmp_path / "site"
    for path in (
        "index.html",
        "404.html",
        "sitemap.xml",
        "sitemap.xml.gz",
        "assets/javascripts/bundle.min.js",
        "assets/javascripts/bundle.min.js.map",
        "assets/javascripts/lunr/min/lunr.de.min.js",
        "assets/javascripts/lunr/tinyseg.js",
    ):
        _write(site / path, "x")
    prune_output(site)
    remaining = sorted(
        path.relative_to(site).as_posix() for path in site.rglob("*") if path.is_file()
    )
    assert remaining == [
        "assets/javascripts/bundle.min.js",
        "index.html",
        "sitemap.xml",
    ]


def test_llms_index_lists_navigation_pages_by_section() -> None:
    pages = [SitePage("README.md", "README.md"), SitePage("docs/A.md", "docs/A.md")]
    text = render_llms(
        site_name="DKC",
        description="Kernel\n packages.",
        site_url=SITE_URL,
        repository_url=REPOSITORY,
        nav=nav_entries([{"Home": "README.md"}, {"Docs": [{"A": "docs/A.md"}]}]),
        pages=pages,
    )
    assert "> Kernel packages." in text
    assert f"## Start here\n\n- Home: {SITE_URL}\n" in text
    assert f"## Docs\n\n- A: {SITE_URL}docs/A.html" in text
    with pytest.raises(SiteError, match="unpublished"):
        render_llms(
            site_name="DKC",
            description="x",
            site_url=SITE_URL,
            repository_url=REPOSITORY,
            nav=nav_entries([{"Missing": "docs/missing.md"}]),
            pages=pages,
        )


def test_fingerprint_formatting_requires_a_v4_fingerprint() -> None:
    assert format_fingerprint(FINGERPRINT.lower() + "\n") == (
        "7B98 D4BE 1341 8D38 BAC0 37D2 7634 9629 CC45 3C26"
    )
    with pytest.raises(SiteError):
        format_fingerprint("7B98")


def test_home_references_point_at_the_site_root(tmp_path: Path) -> None:
    site = tmp_path / "site"
    _write(
        site / "index.html",
        f'<link rel="canonical" href="{SITE_URL}index.html">'
        '<a href="index.html">home</a><a href="docs/index.html">docs</a>',
    )
    _write(
        site / "docs/index.html",
        '<a class="x" href="../index.html#install">install</a>'
        '<a href="index.html">map</a><link rel="prev" href="../index.html">',
    )
    _write(site / "config/flavors/index.html", '<a href="../../index.html">home</a>')
    link_home_to_root(site, SITE_URL)
    assert (site / "index.html").read_text() == (
        f'<link rel="canonical" href="{SITE_URL}">'
        '<a href="./">home</a><a href="docs/index.html">docs</a>'
    )
    assert (site / "docs/index.html").read_text() == (
        '<a class="x" href="../#install">install</a>'
        '<a href="index.html">map</a><link rel="prev" href="../">'
    )
    assert (site / "config/flavors/index.html").read_text() == (
        '<a href="../../">home</a>'
    )


def test_sitemap_lists_public_urls_with_commit_times() -> None:
    pages = [SitePage("docs/A.md", "docs/A.md"), SitePage("README.md", "README.md")]
    dates = parse_page_dates("1700000000\tREADME.md\n1600000000\tdocs/A.md\n")
    text = render_sitemap(SITE_URL, pages, dates)
    assert re.findall(r"<loc>([^<]+)</loc>\s*<lastmod>([^<]+)</lastmod>", text) == [
        (SITE_URL, "2023-11-14T22:13:20+00:00"),
        (f"{SITE_URL}docs/A.html", "2020-09-13T12:26:40+00:00"),
    ]
    with pytest.raises(SiteError, match="modification time"):
        render_sitemap(SITE_URL, pages, {"README.md": 1})
    for record in ("x\tREADME.md", "1 README.md", "1\t../x.md", "1\ta\n2\ta"):
        with pytest.raises(SiteError):
            parse_page_dates(record)


def test_indexnow_keys_follow_the_protocol_alphabet() -> None:
    assert validate_indexnow_key(INDEXNOW_KEY) == INDEXNOW_KEY
    assert validate_indexnow_key((ROOT / "site/indexnow-key.txt").read_text().strip())
    for key in ("short", "x" * 129, "has space here", "-leading-dash", "key.with.dots"):
        with pytest.raises(SiteError):
            validate_indexnow_key(key)
