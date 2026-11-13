# Documentation site

The public site at `https://dkc-linux.romancello.net/` is generated from the
tracked Markdown documentation. The Markdown in this repository remains the
source of truth; the site adds navigation, search, and a landing page without a
second copy of the text.

## Sources

Every tracked Markdown file is a page except `AGENTS.md`, which is
agent-specific, and `TODO.md`. The project `README.md` is the home page.
`.github/workflows/README.md` is published as `ci/README.md` because the
generator skips hidden directories. Every other page keeps its repository path,
with `README.md` becoming `index.html` and `NAME.md` becoming `NAME.html`.

`site/` holds the generator configuration, a small theme override, and one
stylesheet. The navigation in `site/mkdocs.yml` must name every published page
exactly once; a new Markdown file fails the build until it is placed there.

Before the generator runs, links are rewritten for the page's site location:

- a link to another published page stays a relative link;
- a link to any other repository file or directory becomes a GitHub link pinned
  to the exact source revision;
- external links and same-page anchors are unchanged;
- links inside code blocks and inline code are never touched.

The home page is the project README. Its first heading, tagline paragraph, and
the link line directly below the tagline form the landing hero, so keep that
order when editing the README. A test enforces it.

Each page's meta description is its lead paragraph: the first prose paragraph
between the page heading and the first section, without inline markup and
shortened to 160 characters at a sentence or word boundary. A page without
such a paragraph uses the site description. Write the lead paragraph as a
summary that stands on its own in a search result.

## Search engines

- The home page's public address is the site root. Its canonical URL, the
  sitemap, `llms.txt`, and every internal link use `/`, never `/index.html`.
  Every other page uses its `.html` URL.
- `sitemap.xml` lists every page with the commit time of its source as
  `lastmod`. The build therefore needs complete Git history and fails in a
  shallow clone.
- `robots.txt` allows every crawler. Search engines and the fetchers behind
  AI search and user-requested reads are also named in an explicit allow
  group.
- The home page carries `SoftwareSourceCode` structured data that names the
  source repository.
- `site/indexnow-key.txt` holds the public IndexNow key. The build publishes it
  as `<key>.txt` at the site root, where participating search engines verify
  notifications. The key is public by design and is not a credential.

## Build

```sh
make site
```

It is separate from `make fast`; run it after changing any Markdown page or
site input. The target builds offline in a confined container with the
Debian-packaged
MkDocs and Material theme from the toolbox image. The configuration uses no
third-party plugins or Python object tags, so it stays portable to a successor
generator. The build is reproducible for one commit: the source revision and
commit time are inputs.

The result is written to `out/site/<run-id>/`:

| Path | Content |
| --- | --- |
| `site/` | the complete static site |
| `site-manifest.json` | every object's key, SHA-256, size, media type, and cache policy |
| `summary.json` | page, object, byte, and reference counts |
| `evidence.sha256` | checksums of the two records above |

Preview the pages from an HTTP origin, for example
`python3 -m http.server -d out/site/latest/site`. Opened as local files, search
does not work and links to the home page show a directory listing.

The generator output is pruned before the audit: non-English search language
packs, source maps, the custom 404 page, and the compressed sitemap are never
served. The build then points home-page references at the site root and
replaces the generator's sitemap with one dated by commit time.

## Audit

The build fails unless every check passes:

- the generated pages are exactly the published Markdown pages;
- every page has one canonical URL at its public address, one non-empty meta
  description, one `h1`, and a non-empty title;
- no page refers to the home page as `index.html`;
- every same-origin link, script, stylesheet, and image target exists, every
  anchor resolves, and no same-origin reference carries a query string;
- no script, stylesheet, image, or frame loads from another origin, no
  stylesheet imports or loads an external resource, and the third-party
  repository widget is absent;
- every object key is a safe relative path with an allowed suffix, never
  shadows an APT repository path, and stays within the object, size, and count
  bounds;
- `sitemap.xml` lists exactly the pages, each once with a UTC `lastmod`;
  `robots.txt` matches the generated policy; `llms.txt` advertises only
  existing pages; and the IndexNow key file holds the tracked key;
- the home page shows the tracked archive primary fingerprint.

The site carries no analytics, web fonts, or other third-party requests. It is
not a trust anchor: users verify the archive key against the fingerprint
procedure in [USER_INSTALL.md](USER_INSTALL.md).

## Pull requests

The `site` workflow's `documentation site` job runs on every pull request to
`main`. It builds the toolbox image from the pull request's own checkout, so a
generator change is checked too, then runs `make site` and keeps the audited
result as a three-day preview artifact: download it from the workflow run and
open `site/index.html`. The job has no secrets and no path filter, because it is
a required check; see
[MAINTAINER_SETUP.md](MAINTAINER_SETUP.md#required-pull-request-checks).

## Publication

The same workflow publishes on every push to `main` that changes a published
page or a site input, and on a manual dispatch from `main`, in two jobs:

1. **build** resolves the published toolbox image by digest, runs the fast
   tier and then `make site` for the pushed commit, and uploads the audited
   site. It has no secrets.
2. **publish** runs in the `production-storage` Environment. It repeats the
   current-main check with every storage variable removed, so a superseded
   commit never receives the credential, then runs
   `make github-site-publish`.

The publisher verifies the downloaded result against its evidence checksums
and manifest before any request. It acquires the same conditional production
lease as repository publication, waiting up to `SITE_LEASE_WAIT_SECONDS`
(default 1,800 seconds) while a repository publication holds it, then:

1. lists the `site/` storage prefix;
2. writes every new or changed object below `site/` with a conditional
   create or an `If-Match` precondition, assets first and `index.html` last;
3. deletes, by exact key, every `site/` object absent from the new build,
   within the `SITE_MAX_DELETIONS` safety cap (default 400);
4. reads every object back and requires the `site/` listing to equal the
   manifest;
5. releases the lease.

Its summary lists the pages it wrote or deleted.

Unchanged objects are not rewritten. The publisher can write and delete only
keys below `site/`; APT repository keys and controller state are outside its
reach in code. If a run stops midway, the site may briefly mix old and new
pages; the next run converges. A run for a superseded commit fails its
current-main check and leaves publication to the newer run.

Site objects count toward the whole-namespace storage accounting described in
[RETENTION.md](RETENTION.md). They are a few megabytes, well inside the
metadata reserve.

## Search engine notification

After a successful publication, the workflow's `notify IndexNow` job runs:

```sh
make site-indexnow SITE_RESULT=<site-result> SITE_PUBLICATION_RESULT=<publication-result>
```

It verifies both results, waits up to `SITE_INDEXNOW_WAIT_SECONDS` (default
600) until the public site serves the key file and the new bytes of every
changed page, then sends one request to `SITE_INDEXNOW_ENDPOINT` (default
`https://api.indexnow.org/indexnow`) with the changed and removed page URLs.
IndexNow shares it with the participating search engines; Google does not
participate. A page whose edge copy is still older when the wait ends is left
to ordinary sitemap crawling. The log shows the HTTP status and response:
`200` or `202` is accepted, `403` means the key file did not verify, and `422`
means a URL did not match the host. The job needs no secret and never fails
the workflow run.

## Delivery

The delivery layer maps public paths onto the `site/` prefix and caches site
objects at the edge for two hours. Every object carries
`Cache-Control: public, max-age=0, must-revalidate`, so browsers revalidate
against the edge on each visit. A publication is therefore visible within the
edge lifetime without any purge call. The rules and their validation are in
[CLOUDFLARE_CACHE.md](CLOUDFLARE_CACHE.md#7-serve-the-documentation-site).
