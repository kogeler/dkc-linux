#!/usr/bin/env python3
"""Build, prune, and audit the public documentation site offline."""

from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any

import yaml

from dkc import schema
from dkc.serialize import canonical_bytes
from dkc.site import (
    SiteError,
    SiteManifest,
    audit_site,
    manifest_document,
    nav_entries,
    prune_output,
    render_llms,
    render_robots,
    stage_pages,
)

GENERATOR_OUTPUT_LIMIT = 8000


def load_config(path: pathlib.Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SiteError("site configuration is not a mapping")
    extra = value.get("extra")
    for name in ("site_name", "site_description", "site_url"):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise SiteError(f"site configuration lacks {name}")
    if not isinstance(extra, dict) or not isinstance(extra.get("repository_url"), str):
        raise SiteError("site configuration lacks extra.repository_url")
    if not extra["repository_url"].startswith("https://github.com/"):
        raise SiteError("site source repository must be a GitHub HTTPS URL")
    return value


def build(args: argparse.Namespace) -> int:
    repository = args.repository.resolve()
    output = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    config = load_config(repository / "site/mkdocs.yml")
    site_url = config["site_url"]
    repository_url = config["extra"]["repository_url"].rstrip("/")
    fingerprint = (repository / "keys/archive-primary.fingerprint").read_text(
        encoding="ascii"
    )
    site_dir = output / "site"
    with tempfile.TemporaryDirectory(prefix="site-", dir=args.workspace) as directory:
        work = pathlib.Path(directory)
        shutil.copyfile(repository / "site/mkdocs.yml", work / "mkdocs.yml")
        shutil.copytree(repository / "site/theme", work / "theme")
        shutil.copytree(repository / "site/assets", work / "pages/assets")
        pages = stage_pages(
            repository,
            work / "pages",
            repository_url=repository_url,
            revision=args.revision,
        )
        environment = {
            "HOME": os.environ.get("HOME", "/tmp"),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SOURCE_DATE_EPOCH": str(args.source_epoch),
            "TZ": "UTC",
        }
        result = subprocess.run(
            [
                "mkdocs",
                "build",
                "--strict",
                "--config-file",
                str(work / "mkdocs.yml"),
                "--site-dir",
                str(site_dir),
            ],
            cwd=work,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            combined = (result.stdout + result.stderr)[-GENERATOR_OUTPUT_LIMIT:]
            print(combined, file=sys.stderr)
            raise SiteError("the site generator failed")

    prune_output(site_dir)
    nav = nav_entries(config.get("nav"))
    if {entry.target for entry in nav} != {page.target for page in pages}:
        raise SiteError("site navigation must name every published page exactly once")
    (site_dir / "robots.txt").write_text(render_robots(site_url), encoding="utf-8")
    (site_dir / "llms.txt").write_text(
        render_llms(
            site_name=config["site_name"],
            description=config["site_description"],
            site_url=site_url,
            repository_url=repository_url,
            nav=nav,
            pages=pages,
        ),
        encoding="utf-8",
    )
    audit = audit_site(
        site_dir,
        site_url=site_url,
        pages=pages,
        primary_fingerprint=fingerprint,
    )
    document = manifest_document(SiteManifest(site_url, args.revision, audit.objects))
    schema.validate("site-build", document)
    (output / "site-manifest.json").write_bytes(canonical_bytes(document))
    summary = {
        "object_count": len(audit.objects),
        "page_count": audit.pages,
        "reference_count": audit.references,
        "schema": "dkc.site-build-result.v1",
        "size": sum(item.size for item in audit.objects),
        "status": "PASS",
    }
    (output / "summary.json").write_bytes(canonical_bytes(summary))
    records = [
        f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}"
        for name in ("site-manifest.json", "summary.json")
    ]
    (output / "evidence.sha256").write_text("\n".join(records) + "\n", encoding="utf-8")
    print(
        f"PASS site built and audited: {audit.pages} pages, "
        f"{len(audit.objects)} objects, {summary['size']} bytes, "
        f"{audit.references} references"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--workspace", type=pathlib.Path, required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--source-epoch", type=int, required=True)
    args = parser.parse_args()
    if args.source_epoch < 1:
        raise SystemExit("site source epoch must be positive")
    try:
        return build(args)
    except SiteError as exc:
        print(f"FAIL site build: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
