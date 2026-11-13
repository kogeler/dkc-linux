"""Public documentation site: page staging, inventory, manifest, and audit.

The site is generated from the tracked Markdown documentation. Staging maps
repository paths to site paths and rewrites relative links so every published
page keeps working: a link to another published page stays relative, and a
link to anything else becomes a revision-pinned source-repository link. The
audit runs after the generator and fails closed on any page, reference,
inventory, or metadata violation.
"""

from __future__ import annotations

import hashlib
import html
import json
import posixpath
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.sax.saxutils import escape as xml_escape

__all__ = [
    "CONTENT_TYPES",
    "DESCRIPTION_LIMIT",
    "EXCLUDED_PAGES",
    "INDEXNOW_KEY_FILE",
    "MAX_OBJECT_BYTES",
    "MAX_OBJECTS",
    "MAX_TOTAL_BYTES",
    "RENAMED_PAGES",
    "RESERVED_KEYS",
    "RESERVED_PREFIXES",
    "SEARCH_AGENTS",
    "SITE_BUILD_SCHEMA",
    "SITE_CACHE_CONTROL",
    "NavEntry",
    "SiteAudit",
    "SiteError",
    "SiteManifest",
    "SiteObject",
    "SitePage",
    "audit_site",
    "discover_pages",
    "format_fingerprint",
    "inventory",
    "link_home_to_root",
    "manifest_document",
    "nav_entries",
    "page_description",
    "page_url",
    "parse_manifest",
    "parse_page_dates",
    "prune_output",
    "render_llms",
    "render_robots",
    "render_sitemap",
    "rewrite_links",
    "stage_pages",
    "validate_indexnow_key",
    "validate_key",
]


class SiteError(ValueError):
    """The site source or generated output violates the publication contract."""


# Tracked Markdown that is deliberately not published. Every other tracked
# Markdown file becomes a page.
EXCLUDED_PAGES = frozenset({"AGENTS.md", "TODO.md"})

# Pages whose repository path cannot be a site path. The generator skips
# hidden directories, so the workflow guide needs a visible location.
RENAMED_PAGES: Mapping[str, str] = {".github/workflows/README.md": "ci/README.md"}

SITE_BUILD_SCHEMA = "dkc.site-build.v1"

# Every site object revalidates in browsers. The delivery cache applies its own
# edge lifetime, so a publication becomes visible within that bound.
SITE_CACHE_CONTROL = "public, max-age=0, must-revalidate"

CONTENT_TYPES: Mapping[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".txt": "text/plain; charset=utf-8",
    ".xml": "application/xml",
}

# Tracked file holding the public IndexNow key. The build publishes it as
# `<key>.txt` at the site root, where IndexNow search engines verify it.
INDEXNOW_KEY_FILE = "site/indexnow-key.txt"

# Search engines and the fetchers behind AI search and user-requested reads.
# robots.txt names them in an explicit allow group, so a narrower default
# group added later cannot exclude them by accident.
SEARCH_AGENTS = (
    "Googlebot",
    "Bingbot",
    "OAI-SearchBot",
    "ChatGPT-User",
    "Claude-SearchBot",
    "Claude-User",
    "PerplexityBot",
    "Perplexity-User",
)

# Longest meta description taken from a page's lead paragraph.
DESCRIPTION_LIMIT = 160

MAX_OBJECTS = 400
MAX_OBJECT_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_KEY_LENGTH = 160

# Public repository paths that the delivery layer serves from the APT
# namespace. A site key must never shadow one of them.
RESERVED_PREFIXES = ("dists/", "pool/")
RESERVED_KEYS = frozenset(
    {
        "keys/archive-primary.fingerprint",
        "keys/archive-signing-subkeys.fingerprints",
        "keys/dkc-archive-keyring.gpg",
    }
)

# Generator output that the published site does not need. Lunr language
# packs load only for non-English search, source maps are developer aids, the
# origin never serves a custom 404 page, and the plain sitemap suffices.
_PRUNED_DIRECTORIES = ("assets/javascripts/lunr",)
_PRUNED_FILES = ("404.html", "sitemap.xml.gz")
_PRUNED_SUFFIXES = (".map",)

_SEGMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_CODE_SPAN_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.DOTALL)
_LINK_RE = re.compile(r"(?<![!\\])\[(?P<text>[^\[\]]*)\]\((?P<target>[^()\s]+)\)")
_HTTPS_URL_RE = re.compile(r"https://[^\s)>\]]+")
_CSS_URL_RE = re.compile(r"url\(\s*['\"]?\s*([^)'\"\s]*)", re.I)
_INDEXNOW_KEY_RE = re.compile(r"[A-Za-z0-9-]{8,128}")
_EPOCH_RE = re.compile(r"[1-9][0-9]*")
_HREF_RE = re.compile(r'(?P<prefix>\shref=")(?P<value>[^"]*)"')
_LASTMOD_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")


@dataclass(frozen=True)
class SitePage:
    """One tracked Markdown file and its location in the generated site."""

    source: str
    target: str

    @property
    def url_path(self) -> str:
        directory, name = posixpath.split(self.target)
        document = "index.html" if name == "README.md" else f"{name[:-3]}.html"
        return posixpath.join(directory, document) if directory else document


def page_url(site_url: str, url_path: str) -> str:
    """Return the public URL of one page; the home page is the site root."""

    return site_url if url_path == "index.html" else urljoin(site_url, url_path)


def _check_relative(path: str, label: str) -> str:
    parts = path.split("/")
    if (
        not path
        or path.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise SiteError(f"{label} is not a safe relative path: {path!r}")
    return path


def discover_pages(root: Path) -> tuple[SitePage, ...]:
    """Map every published Markdown file below one source tree."""

    pages: list[SitePage] = []
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink() or not path.is_file():
            raise SiteError(f"Markdown source is not a regular file: {path}")
        source = path.relative_to(root).as_posix()
        if source in EXCLUDED_PAGES:
            continue
        target = RENAMED_PAGES.get(source, source)
        _check_relative(target, "site page")
        if any(part.startswith(".") for part in target.split("/")):
            raise SiteError(f"hidden Markdown source has no site mapping: {source}")
        pages.append(SitePage(source, target))
    sources = {page.source for page in pages}
    missing = sorted(set(RENAMED_PAGES) - sources)
    if missing:
        raise SiteError(f"renamed site pages are missing: {missing}")
    targets = [page.url_path.lower() for page in pages]
    if len(set(targets)) != len(targets):
        raise SiteError("two Markdown sources map to the same site page")
    if not any(page.target == "README.md" for page in pages):
        raise SiteError("the root README.md must be the site home page")
    return tuple(pages)


def _protected_ranges(text: str) -> list[tuple[int, int]]:
    """Return fenced-block and code-span ranges where links are literal."""

    ranges: list[tuple[int, int]] = []
    open_segment = 0
    offset = 0
    fence: str | None = None
    fence_start = 0
    segments: list[tuple[int, int]] = []
    for line in text.splitlines(keepends=True):
        match = _FENCE_RE.match(line)
        if fence is None and match is not None:
            segments.append((open_segment, offset))
            fence = match.group(1)
            fence_start = offset
        elif (
            fence is not None
            and match is not None
            and match.group(1)[0] == fence[0]
            and len(match.group(1)) >= len(fence)
            and not line.strip()[len(match.group(1)) :].strip()
        ):
            ranges.append((fence_start, offset + len(line)))
            fence = None
            open_segment = offset + len(line)
        offset += len(line)
    if fence is not None:
        ranges.append((fence_start, len(text)))
    else:
        segments.append((open_segment, len(text)))
    for start, end in segments:
        for span in _CODE_SPAN_RE.finditer(text, start, end):
            ranges.append(span.span())
    return ranges


def rewrite_links(
    text: str,
    *,
    page: SitePage,
    pages_by_source: Mapping[str, SitePage],
    source_kind: Callable[[str], str | None],
    repository_url: str,
    revision: str,
) -> str:
    """Rewrite relative Markdown links for the page's site location.

    `source_kind` returns "file", "directory", or None for a repository path.
    """

    if not _REVISION_RE.fullmatch(revision):
        raise SiteError("site source revision must be a full Git SHA")
    protected = _protected_ranges(text)

    def literal(position: int) -> bool:
        return any(start <= position < end for start, end in protected)

    def replace(match: re.Match[str]) -> str:
        if literal(match.start()) or literal(match.start("target")):
            return match.group(0)
        target = match.group("target")
        if target.startswith("#") or _SCHEME_RE.match(target):
            return match.group(0)
        if target.startswith("/"):
            raise SiteError(f"{page.source}: absolute link is not portable: {target}")
        path, separator, fragment = target.partition("#")
        resolved = posixpath.normpath(
            posixpath.join(posixpath.dirname(page.source), unquote(path))
        )
        if resolved == ".." or resolved.startswith("../"):
            raise SiteError(f"{page.source}: link escapes the repository: {target}")
        linked = pages_by_source.get(resolved)
        if linked is not None:
            base = posixpath.dirname(page.target) or "."
            rewritten = posixpath.relpath(linked.target, base)
        else:
            kind = source_kind(resolved)
            if kind is None:
                raise SiteError(f"{page.source}: link target does not exist: {target}")
            route = "tree" if kind == "directory" else "blob"
            rewritten = f"{repository_url}/{route}/{revision}/{quote(resolved)}"
        return f"[{match.group('text')}]({rewritten}{separator}{fragment})"

    return _LINK_RE.sub(replace, text)


_INLINE_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_INLINE_MARKUP_RE = (
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])"),
    re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"),
    re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"),
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_NON_PARAGRAPH_RE = re.compile(r"(?:#|\||<|>|[-*+]\s|\d+[.)]\s|\s)")
_SECTION_RE = re.compile(r"#{2,6}\s")


def _plain_text(markdown: str) -> str:
    text = _CODE_SPAN_RE.sub(lambda match: match.group(2).strip(), markdown)
    text = _INLINE_LINK_RE.sub(r"\1", text)
    for pattern in _INLINE_MARKUP_RE:
        text = pattern.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    text = re.sub(r"\\([\\`*_{}\[\]()#+.!|-])", r"\1", text)
    return " ".join(text.split())


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    kept = ""
    for sentence in _SENTENCE_END_RE.split(text):
        candidate = f"{kept} {sentence}".strip()
        if len(candidate) > limit:
            break
        kept = candidate
    # Whole sentences are preferred unless they would leave half the space
    # unused; then the text is cut at a word boundary.
    if len(kept) >= limit // 2:
        return kept
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "\u2026"


def page_description(text: str) -> str | None:
    """Return a page's lead paragraph as plain text for its meta description.

    Only the introduction between the page heading and the first section is
    considered. The paragraph is used verbatim without its inline markup and is
    shortened at a sentence or word boundary. None means the page has no prose
    introduction, so the generator falls back to the site description.
    """

    paragraph: list[str] = []
    fence: str | None = None
    for line in [*text.splitlines(), ""]:
        match = _FENCE_RE.match(line)
        if fence is not None:
            if match is not None and match.group(1)[0] == fence[0]:
                fence = None
            continue
        if match is not None:
            fence = match.group(1)
            paragraph = []
            continue
        if _SECTION_RE.match(line):
            return None
        if line.strip():
            if paragraph or not _NON_PARAGRAPH_RE.match(line):
                paragraph.append(line)
            continue
        if paragraph:
            plain = _plain_text(" ".join(paragraph))
            if len(plain.split()) >= 5:
                return _shorten(plain, DESCRIPTION_LIMIT)
            paragraph = []
    return None


def _front_matter(page: SitePage, text: str) -> str:
    lines: list[str] = []
    if page.target == "README.md":
        lines.extend(("hide:", "  - navigation"))
    description = page_description(text)
    if description is not None:
        # The generator's templates do not autoescape, so the value is stored
        # as HTML text. A JSON string is also a valid YAML scalar.
        escaped = json.dumps(html.escape(description), ensure_ascii=False)
        lines.append(f"description: {escaped}")
    return "---\n" + "\n".join(lines) + "\n---\n\n" if lines else ""


def stage_pages(
    root: Path,
    destination: Path,
    *,
    repository_url: str,
    revision: str,
) -> tuple[SitePage, ...]:
    """Write every published page below `destination` with rewritten links."""

    pages = discover_pages(root)
    by_source = {page.source: page for page in pages}

    def source_kind(path: str) -> str | None:
        candidate = root / path
        if candidate.is_symlink():
            return None
        if candidate.is_file():
            return "file"
        if candidate.is_dir():
            return "directory"
        return None

    for page in pages:
        text = (root / page.source).read_text(encoding="utf-8")
        if text.startswith("---\n"):
            raise SiteError(
                f"{page.source}: front matter is reserved for the generator"
            )
        staged = rewrite_links(
            text,
            page=page,
            pages_by_source=by_source,
            source_kind=source_kind,
            repository_url=repository_url,
            revision=revision,
        )
        staged = _front_matter(page, text) + staged
        output = destination / page.target
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(staged, encoding="utf-8")
    return pages


def prune_output(site_dir: Path) -> None:
    """Remove generator output that is never served."""

    for directory in _PRUNED_DIRECTORIES:
        path = site_dir / directory
        if path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_dir() and not child.is_symlink():
                    child.rmdir()
                else:
                    child.unlink()
            path.rmdir()
    for name in _PRUNED_FILES:
        (site_dir / name).unlink(missing_ok=True)
    for suffix in _PRUNED_SUFFIXES:
        for path in sorted(site_dir.rglob(f"*{suffix}")):
            path.unlink()


def link_home_to_root(site_dir: Path, site_url: str) -> None:
    """Point every reference to the home page at the site root.

    The generator names the home page `index.html`. Its public address is the
    site root, so the canonical URL, the sitemap, and every internal link use
    that one form. Same-page fragments are preserved.
    """

    for path in sorted(site_dir.rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        updated = _link_home(text, path.relative_to(site_dir).as_posix(), site_url)
        if updated != text:
            path.write_text(updated, encoding="utf-8")


def _link_home(text: str, key: str, site_url: str) -> str:
    home = urljoin(site_url, "index.html")
    location = urljoin(site_url, key)
    root = "../" * key.count("/") or "./"

    def replace(match: re.Match[str]) -> str:
        value = match.group("value")
        target = urljoin(location, html.unescape(value)).partition("#")[0]
        if target != home:
            return match.group(0)
        _, separator, fragment = value.partition("#")
        absolute = value.startswith("//") or _SCHEME_RE.match(value)
        base = site_url if absolute else root
        return f'{match.group("prefix")}{base}{separator}{fragment}"'

    return _HREF_RE.sub(replace, text)


@dataclass(frozen=True)
class NavEntry:
    section: str | None
    title: str
    target: str


def nav_entries(nav: object) -> tuple[NavEntry, ...]:
    """Flatten the generator navigation into page entries.

    External links are not pages and are skipped.
    """

    entries: list[NavEntry] = []

    def walk(items: object, section: str | None) -> None:
        if not isinstance(items, list):
            raise SiteError("site navigation must be a list")
        for item in items:
            if not isinstance(item, dict) or len(item) != 1:
                raise SiteError("each navigation item must be one titled entry")
            ((title, value),) = item.items()
            if not isinstance(title, str) or not title.strip():
                raise SiteError("navigation titles must be non-empty text")
            if isinstance(value, list):
                walk(value, title if section is None else f"{section} / {title}")
            elif isinstance(value, str):
                if _SCHEME_RE.match(value):
                    continue
                entries.append(NavEntry(section, title, _check_relative(value, "nav")))
            else:
                raise SiteError(f"navigation entry {title!r} is malformed")

    walk(nav, None)
    targets = [entry.target for entry in entries]
    if len(set(targets)) != len(targets):
        raise SiteError("a page appears more than once in the site navigation")
    return tuple(entries)


def render_robots(site_url: str) -> str:
    agents = "".join(f"User-agent: {agent}\n" for agent in SEARCH_AGENTS)
    return (
        "User-agent: *\nAllow: /\n\n"
        f"{agents}Allow: /\n\n"
        f"Sitemap: {site_url}sitemap.xml\n"
    )


def parse_page_dates(text: str) -> dict[str, int]:
    """Parse `<epoch>TAB<repository path>` last-commit records."""

    dates: dict[str, int] = {}
    for line in text.splitlines():
        epoch, separator, path = line.partition("\t")
        if not separator or not _EPOCH_RE.fullmatch(epoch) or path in dates:
            raise SiteError(f"page date record is malformed: {line!r}")
        dates[_check_relative(path, "page date path")] = int(epoch)
    return dates


def render_sitemap(
    site_url: str, pages: Sequence[SitePage], modified: Mapping[str, int]
) -> str:
    """List every page at its public URL with its source's last commit time."""

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    ordered = sorted(
        pages, key=lambda page: (page.url_path != "index.html", page.url_path)
    )
    for page in ordered:
        epoch = modified.get(page.source)
        if epoch is None or epoch < 1:
            raise SiteError(f"page has no modification time: {page.source}")
        stamp = datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        lines.extend(
            (
                "  <url>",
                f"    <loc>{xml_escape(page_url(site_url, page.url_path))}</loc>",
                f"    <lastmod>{stamp}</lastmod>",
                "  </url>",
            )
        )
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def validate_indexnow_key(value: str) -> str:
    """Require one IndexNow key: 8 to 128 letters, digits, or dashes."""

    if not _INDEXNOW_KEY_RE.fullmatch(value) or value.startswith("-"):
        raise SiteError("IndexNow key must be 8-128 letters, digits, or dashes")
    return value


def render_llms(
    *,
    site_name: str,
    description: str,
    site_url: str,
    repository_url: str,
    nav: Sequence[NavEntry],
    pages: Sequence[SitePage],
) -> str:
    """Render a plain-text page index for automated readers."""

    by_target = {page.target: page for page in pages}
    lines = [
        f"# {site_name}",
        "",
        f"> {' '.join(description.split())}",
        "",
        f"Documentation: {site_url}",
        f"Source: {repository_url}",
    ]
    section: object = object()
    for entry in nav:
        page = by_target.get(entry.target)
        if page is None:
            raise SiteError(f"navigation names an unpublished page: {entry.target}")
        if entry.section != section:
            section = entry.section
            lines.extend(("", f"## {entry.section or 'Start here'}", ""))
        lines.append(f"- {entry.title}: {page_url(site_url, page.url_path)}")
    return "\n".join(lines) + "\n"


def format_fingerprint(value: str) -> str:
    """Format one OpenPGP v4 fingerprint as ten groups of four."""

    normalized = value.strip().upper()
    if not re.fullmatch(r"[0-9A-F]{40}", normalized):
        raise SiteError("archive primary fingerprint is malformed")
    return " ".join(normalized[index : index + 4] for index in range(0, 40, 4))


def validate_key(key: str) -> str:
    """Require one safe, published, non-reserved site object key."""

    if len(key) > MAX_KEY_LENGTH:
        raise SiteError(f"site object key is too long: {key}")
    parts = key.split("/")
    if not all(_SEGMENT_RE.fullmatch(part) for part in parts):
        raise SiteError(f"site object key is unsafe: {key!r}")
    if key.startswith(RESERVED_PREFIXES) or key in RESERVED_KEYS:
        raise SiteError(f"site object key shadows a repository path: {key}")
    if posixpath.splitext(key)[1] not in CONTENT_TYPES:
        raise SiteError(f"site object type is not allowed: {key}")
    return key


@dataclass(frozen=True)
class SiteObject:
    key: str
    sha256: str
    size: int
    content_type: str
    cache_control: str = SITE_CACHE_CONTROL

    def __post_init__(self) -> None:
        validate_key(self.key)
        if not _SHA256_RE.fullmatch(self.sha256):
            raise SiteError(f"site object digest is malformed: {self.key}")
        if not 0 < self.size <= MAX_OBJECT_BYTES:
            raise SiteError(f"site object size is out of bounds: {self.key}")
        if self.content_type != CONTENT_TYPES[posixpath.splitext(self.key)[1]]:
            raise SiteError(f"site object media type is inconsistent: {self.key}")
        if self.cache_control != SITE_CACHE_CONTROL:
            raise SiteError(f"site object cache policy is inconsistent: {self.key}")

    def to_dict(self) -> dict[str, object]:
        return {
            "cache_control": self.cache_control,
            "content_type": self.content_type,
            "key": self.key,
            "sha256": self.sha256,
            "size": self.size,
        }


def _check_totals(objects: Sequence[SiteObject]) -> None:
    if not objects or len(objects) > MAX_OBJECTS:
        raise SiteError("site object count is out of bounds")
    if sum(item.size for item in objects) > MAX_TOTAL_BYTES:
        raise SiteError("site size exceeds its bound")
    if [item.key for item in objects] != sorted({item.key for item in objects}):
        raise SiteError("site objects must be unique and sorted by key")
    if "index.html" not in {item.key for item in objects}:
        raise SiteError("site has no root page")


def inventory(site_dir: Path) -> tuple[SiteObject, ...]:
    """Describe every generated file as one publishable object."""

    objects: list[SiteObject] = []
    for path in sorted(site_dir.rglob("*")):
        if path.is_symlink():
            raise SiteError(f"generated site contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise SiteError(f"generated site contains a special file: {path}")
        key = path.relative_to(site_dir).as_posix()
        body = path.read_bytes()
        objects.append(
            SiteObject(
                key=validate_key(key),
                sha256=hashlib.sha256(body).hexdigest(),
                size=len(body),
                content_type=CONTENT_TYPES[posixpath.splitext(key)[1]],
            )
        )
    objects.sort(key=lambda item: item.key)
    _check_totals(objects)
    return tuple(objects)


@dataclass(frozen=True)
class SiteManifest:
    site_url: str
    source_revision: str
    objects: tuple[SiteObject, ...]

    def __post_init__(self) -> None:
        _check_site_url(self.site_url)
        if not _REVISION_RE.fullmatch(self.source_revision):
            raise SiteError("site source revision must be a full Git SHA")
        _check_totals(self.objects)


def _check_site_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.path != "/"
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise SiteError("site URL must be an HTTPS origin ending in '/'")
    return value


def manifest_document(manifest: SiteManifest) -> dict[str, object]:
    return {
        "objects": [item.to_dict() for item in manifest.objects],
        "schema": SITE_BUILD_SCHEMA,
        "site_url": manifest.site_url,
        "source_revision": manifest.source_revision,
    }


def parse_manifest(value: object) -> SiteManifest:
    """Parse the exact site-build manifest; unknown fields fail closed."""

    if not isinstance(value, dict) or set(value) != {
        "objects",
        "schema",
        "site_url",
        "source_revision",
    }:
        raise SiteError("site manifest has an invalid field set")
    if value["schema"] != SITE_BUILD_SCHEMA:
        raise SiteError("site manifest has an unsupported schema")
    raw_objects = value["objects"]
    site_url = value["site_url"]
    revision = value["source_revision"]
    if not isinstance(raw_objects, list) or not isinstance(site_url, str):
        raise SiteError("site manifest fields have invalid types")
    if not isinstance(revision, str):
        raise SiteError("site manifest revision is invalid")
    objects: list[SiteObject] = []
    fields = {"cache_control", "content_type", "key", "sha256", "size"}
    for raw in raw_objects:
        if not isinstance(raw, dict) or set(raw) != fields:
            raise SiteError("site manifest object has an invalid field set")
        size = raw["size"]
        strings = [
            raw[name] for name in ("cache_control", "content_type", "key", "sha256")
        ]
        if isinstance(size, bool) or not isinstance(size, int):
            raise SiteError("site manifest object size is invalid")
        if not all(isinstance(item, str) for item in strings):
            raise SiteError("site manifest object fields have invalid types")
        objects.append(
            SiteObject(
                key=raw["key"],
                sha256=raw["sha256"],
                size=size,
                content_type=raw["content_type"],
                cache_control=raw["cache_control"],
            )
        )
    return SiteManifest(site_url, revision, tuple(objects))


@dataclass
class _Document:
    ids: set[str] = field(default_factory=set)
    references: list[tuple[str, str]] = field(default_factory=list)
    canonicals: list[str] = field(default_factory=list)
    descriptions: list[str] = field(default_factory=list)
    h1_count: int = 0
    title: list[str] = field(default_factory=list)
    text: list[str] = field(default_factory=list)
    source_component: bool = False


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.document = _Document()
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        if identifier := values.get("id"):
            self.document.ids.add(identifier)
        if tag == "h1":
            self.document.h1_count += 1
        if tag == "title":
            self._in_title = True
        if values.get("data-md-component") == "source":
            self.document.source_component = True
        if tag == "meta" and values.get("name", "").lower() == "description":
            self.document.descriptions.append(values.get("content", ""))
        if tag == "link" and "canonical" in values.get("rel", "").split():
            self.document.canonicals.append(values.get("href", ""))
            return
        for attribute in ("href", "src"):
            if attribute in values:
                self.document.references.append((tag, values[attribute]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        self.document.text.append(data)
        if self._in_title:
            self.document.title.append(data)


def _parse_page(path: Path) -> _Document:
    parser = _PageParser()
    parser.feed(path.read_text(encoding="utf-8"))
    parser.close()
    return parser.document


@dataclass(frozen=True)
class SiteAudit:
    objects: tuple[SiteObject, ...]
    pages: int
    references: int


def _reserved_public_path(path: str) -> bool:
    return path.startswith(RESERVED_PREFIXES) or path in RESERVED_KEYS


def audit_site(
    site_dir: Path,
    *,
    site_url: str,
    pages: Sequence[SitePage],
    primary_fingerprint: str,
    indexnow_key: str,
) -> SiteAudit:
    """Validate the complete generated site without network access."""

    _check_site_url(site_url)
    origin = urlsplit(site_url)
    objects = inventory(site_dir)
    keys = {item.key for item in objects}
    expected_pages = {page.url_path for page in pages}
    html_keys = {key for key in keys if key.endswith(".html")}
    if html_keys != expected_pages:
        raise SiteError(
            "generated pages differ from the source pages: "
            f"missing={sorted(expected_pages - html_keys)}, "
            f"extra={sorted(html_keys - expected_pages)}"
        )
    if any(key.endswith(".md") for key in keys):
        raise SiteError("generated site contains Markdown source")

    documents = {key: _parse_page(site_dir / key) for key in sorted(html_keys)}
    checked = 0
    for key, document in documents.items():
        public_url = page_url(site_url, key)
        if document.canonicals != [public_url]:
            raise SiteError(
                f"{key}: canonical {document.canonicals!r} != {public_url!r}"
            )
        if len(document.descriptions) != 1 or not document.descriptions[0].strip():
            raise SiteError(f"{key}: expected one non-empty meta description")
        if document.h1_count != 1:
            raise SiteError(f"{key}: expected one h1, found {document.h1_count}")
        if not "".join(document.title).strip():
            raise SiteError(f"{key}: empty title")
        if document.source_component:
            raise SiteError(f"{key}: repository widget would contact a third party")
        for tag, reference in document.references:
            checked += 1
            _check_reference(
                site_dir,
                documents,
                key=key,
                location=urljoin(site_url, key),
                origin=(origin.scheme, origin.netloc),
                tag=tag,
                reference=reference,
            )

    for key in sorted(keys):
        if key.endswith(".css"):
            css = (site_dir / key).read_text(encoding="utf-8")
            if "@import" in css.lower() or any(
                (value.startswith("//") or _SCHEME_RE.match(value))
                and not value.lower().startswith("data:")
                for value in _CSS_URL_RE.findall(css)
            ):
                raise SiteError(f"{key}: stylesheet loads an external resource")

    page_urls = {page_url(site_url, path) for path in html_keys}
    _check_sitemap(site_dir / "sitemap.xml", page_urls)
    robots = (site_dir / "robots.txt").read_text(encoding="utf-8")
    if robots != render_robots(site_url):
        raise SiteError("robots.txt does not match the generated policy")
    llms = (site_dir / "llms.txt").read_text(encoding="utf-8")
    advertised = {url.rstrip(".,") for url in _HTTPS_URL_RE.findall(llms)}
    local = {url for url in advertised if url.startswith(site_url) and url != site_url}
    if not local or not local <= page_urls:
        raise SiteError("llms.txt advertises a page that does not exist")
    key_file = site_dir / f"{validate_indexnow_key(indexnow_key)}.txt"
    if not key_file.is_file() or key_file.read_text(encoding="ascii") != indexnow_key:
        raise SiteError("the IndexNow key file is missing or does not hold the key")

    fingerprint = format_fingerprint(primary_fingerprint)
    if fingerprint not in "".join(documents["index.html"].text):
        raise SiteError("home page does not show the tracked archive fingerprint")
    return SiteAudit(objects, len(documents), checked)


def _check_reference(
    site_dir: Path,
    documents: Mapping[str, _Document],
    *,
    key: str,
    location: str,
    origin: tuple[str, str],
    tag: str,
    reference: str,
) -> None:
    value = reference.strip()
    if tag in ("img", "source") and value.startswith("data:"):
        return
    if value.startswith(("mailto:", "tel:")) and tag == "a":
        return
    if value.lower().startswith(("javascript:", "data:")):
        raise SiteError(f"{key}: executable or inline reference: {value[:40]}")
    target = urlsplit(urljoin(location, value))
    if (target.scheme, target.netloc) != origin:
        if tag != "a":
            raise SiteError(f"{key}: {tag} loads an external resource: {value}")
        return
    if target.query:
        raise SiteError(f"{key}: same-origin reference carries a query: {value}")
    path = unquote(target.path).lstrip("/")
    if _reserved_public_path(path):
        return
    if path == "index.html":
        raise SiteError(f"{key}: home page reference must use the site root: {value}")
    if path == "":
        path = "index.html"
    if path.endswith("/"):
        raise SiteError(f"{key}: directory reference has no index document: {value}")
    if path not in documents and not (site_dir / path).is_file():
        raise SiteError(f"{key}: {tag} target does not exist: {value}")
    if (
        target.fragment
        and path.endswith(".html")
        and unquote(target.fragment) not in documents[path].ids
    ):
        raise SiteError(f"{key}: missing anchor in {value}")


def _check_sitemap(path: Path, expected: set[str]) -> None:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise SiteError("sitemap.xml is missing or invalid") from exc
    namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    entries = root.findall("s:url", namespace)
    actual = {(entry.findtext("s:loc", "", namespace)).strip() for entry in entries}
    if len(actual) != len(entries) or any(
        not _LASTMOD_RE.fullmatch(entry.findtext("s:lastmod", "", namespace).strip())
        for entry in entries
    ):
        raise SiteError("sitemap entries must be unique and carry a UTC lastmod")
    if actual != expected:
        raise SiteError(
            "sitemap differs from the generated pages: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
