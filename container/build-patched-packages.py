#!/usr/bin/env python3
"""Rebuild the Debian packages listed in the patched-package ledger.

Runs in a disposable stage of the kernel build image. For every fix, in ledger
order, it selects the newest version of each listed source across every
configured Debian suite, applies the fix's patches with Debian's fuzz-free
quilt, rebuilds the binaries with a DKC version suffix, and makes them
available to the sources built after it. The binaries a fix names in `install`
are copied to the output directory together with a provenance manifest.

The script is generic: it knows no package, version, or patch.
"""

from __future__ import annotations

import argparse
import email.utils
import pathlib
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PATCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.patch$")
_LOCAL_REPO = pathlib.Path("/var/local/dkc-patched-repo")
_MANIFEST_HEADER = (
    "fix",
    "source",
    "debian_version",
    "package",
    "version",
    "architecture",
    "sha256",
)


class LedgerError(ValueError):
    """The ledger is malformed."""


@dataclass(frozen=True)
class Patch:
    name: str
    diff: str


@dataclass(frozen=True)
class Source:
    package: str
    patches: tuple[Patch, ...]


@dataclass(frozen=True)
class Fix:
    id: str
    revision: int
    obsolete_from_upstream_version: str | None
    install: tuple[str, ...]
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class Ledger:
    fixes: tuple[Fix, ...]


def _require(value: object, kind: type, where: str) -> object:
    if not isinstance(value, kind) or isinstance(value, bool) and kind is not bool:
        raise LedgerError(f"{where} must be a {kind.__name__}")
    return value


def parse_ledger(data: dict[str, object]) -> Ledger:
    """Validate the complete ledger before anything is downloaded or built."""

    allowed_top = {"schema_version", "fix"}
    if set(data) - allowed_top:
        raise LedgerError(f"unknown ledger keys: {sorted(set(data) - allowed_top)}")
    if data.get("schema_version") != 1:
        raise LedgerError("schema_version must be 1")
    raw_fixes = data.get("fix", [])
    _require(raw_fixes, list, "fix")
    assert isinstance(raw_fixes, list)
    fixes: list[Fix] = []
    seen_ids: set[str] = set()
    seen_sources: set[str] = set()
    for index, raw in enumerate(raw_fixes):
        where = f"fix[{index}]"
        _require(raw, dict, where)
        allowed = {
            "id",
            "revision",
            "problem",
            "upstream",
            "obsolete_from_upstream_version",
            "install",
            "source",
        }
        if set(raw) - allowed:
            raise LedgerError(f"{where} has unknown keys: {sorted(set(raw) - allowed)}")
        fix_id = _require(raw.get("id"), str, f"{where}.id")
        assert isinstance(fix_id, str)
        if not _ID_RE.fullmatch(fix_id) or fix_id in seen_ids:
            raise LedgerError(f"{where}.id is unsafe or duplicated")
        seen_ids.add(fix_id)
        for text_key in ("problem", "upstream"):
            text = _require(raw.get(text_key), str, f"{fix_id}.{text_key}")
            assert isinstance(text, str)
            if not text.strip():
                raise LedgerError(f"{fix_id}.{text_key} must explain the fix")
        revision = _require(raw.get("revision"), int, f"{fix_id}.revision")
        assert isinstance(revision, int)
        if revision < 1:
            raise LedgerError(f"{fix_id}.revision must be positive")
        obsolete = raw.get("obsolete_from_upstream_version")
        if obsolete is not None:
            _require(obsolete, str, f"{fix_id}.obsolete_from_upstream_version")
        install = _require(raw.get("install"), list, f"{fix_id}.install")
        assert isinstance(install, list)
        if not install or not all(
            isinstance(name, str) and _NAME_RE.fullmatch(name) for name in install
        ):
            raise LedgerError(f"{fix_id}.install must name binary packages")
        raw_sources = _require(raw.get("source"), list, f"{fix_id}.source")
        assert isinstance(raw_sources, list)
        if not raw_sources:
            raise LedgerError(f"{fix_id} lists no source package")
        sources: list[Source] = []
        for source_index, raw_source in enumerate(raw_sources):
            source_where = f"{fix_id}.source[{source_index}]"
            _require(raw_source, dict, source_where)
            if set(raw_source) - {"package", "patch"}:
                raise LedgerError(f"{source_where} has unknown keys")
            package = _require(raw_source.get("package"), str, f"{source_where}.package")
            assert isinstance(package, str)
            if not _NAME_RE.fullmatch(package) or package in seen_sources:
                raise LedgerError(f"{source_where}.package is unsafe or duplicated")
            seen_sources.add(package)
            patches: list[Patch] = []
            for raw_patch in raw_source.get("patch", []):
                _require(raw_patch, dict, f"{source_where}.patch")
                if set(raw_patch) != {"name", "diff"}:
                    raise LedgerError(f"{source_where}.patch needs exactly name and diff")
                name, diff = raw_patch["name"], raw_patch["diff"]
                if not isinstance(name, str) or not _PATCH_RE.fullmatch(name):
                    raise LedgerError(f"{source_where}.patch name is unsafe")
                if not isinstance(diff, str) or "\n--- " not in f"\n{diff}":
                    raise LedgerError(f"{source_where}.patch {name} is not a diff")
                patches.append(Patch(name, diff))
            sources.append(Source(package, tuple(patches)))
        if not any(source.patches for source in sources):
            raise LedgerError(f"{fix_id} carries no patch")
        fixes.append(
            Fix(
                fix_id,
                revision,
                obsolete if isinstance(obsolete, str) else None,
                tuple(install),
                tuple(sources),
            )
        )
    return Ledger(tuple(fixes))


def load_ledger(path: pathlib.Path) -> Ledger:
    with path.open("rb") as stream:
        return parse_ledger(tomllib.load(stream))


def rebuilt_version(version: str, revision: int) -> str:
    """A suffix that sorts above its Debian version and below the next upload."""

    return f"{version}+dkc{revision}"


def upstream_version(version: str) -> str:
    without_epoch = version.split(":", 1)[1] if ":" in version else version
    return without_epoch.rsplit("-", 1)[0] if "-" in without_epoch else without_epoch


def version_gt(left: str, right: str) -> bool:
    return (
        subprocess.run(
            ["dpkg", "--compare-versions", left, "gt", right], check=False
        ).returncode
        == 0
    )


def newest_source_version(package: str) -> str:
    """The newest version of a source across every configured deb-src suite."""

    output = subprocess.run(
        ["apt-cache", "showsrc", "--only-source", package],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    versions = sorted(set(re.findall(r"(?m)^Version: (\S+)$", output)))
    if not versions:
        raise RuntimeError(f"no Debian source found for {package}")
    best = versions[0]
    for version in versions[1:]:
        if version_gt(version, best):
            best = version
    return best


def run(command: list[str], *, cwd: pathlib.Path | None = None, log: pathlib.Path) -> None:
    with log.open("a", encoding="utf-8") as stream:
        result = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.STDOUT)
    if result.returncode:
        tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        sys.stderr.write("\n".join(tail) + "\n")
        raise RuntimeError(f"{' '.join(command[:2])} failed with {result.returncode}")


def prepend_changelog(tree: pathlib.Path, version: str, fix: Fix) -> None:
    """Add one entry that reuses the previous entry's date for reproducibility."""

    changelog = tree / "debian" / "changelog"
    text = changelog.read_text(encoding="utf-8")
    first = text.splitlines()[0]
    match = re.match(r"^(\S+) \(", first)
    trailer = re.search(r"(?m)^ -- .+?>  (.+)$", text)
    if match is None or trailer is None:
        raise RuntimeError(f"cannot parse the changelog of {tree.name}")
    stamp = email.utils.format_datetime(email.utils.parsedate_to_datetime(trailer[1]))
    entry = (
        f"{match[1]} ({version}) trixie; urgency=medium\n\n"
        f"  * Rebuild with DKC fix {fix.id}.\n\n"
        f" -- DKC Build Service <build@dkc.invalid>  {stamp}\n\n"
    )
    changelog.write_text(entry + text, encoding="utf-8")


def add_patches(tree: pathlib.Path, patches: tuple[Patch, ...]) -> None:
    if not patches:
        return
    source_format = (tree / "debian" / "source" / "format").read_text().strip()
    if source_format != "3.0 (quilt)":
        raise RuntimeError(f"{tree.name} is not a 3.0 (quilt) source")
    patch_dir = tree / "debian" / "patches"
    patch_dir.mkdir(exist_ok=True)
    series = patch_dir / "series"
    existing = series.read_text().splitlines() if series.exists() else []
    for patch in patches:
        if (patch_dir / patch.name).exists():
            raise RuntimeError(f"{tree.name} already has a patch named {patch.name}")
        (patch_dir / patch.name).write_text(patch.diff, encoding="utf-8")
        existing.append(patch.name)
    series.write_text("\n".join(existing) + "\n")


def publish_local_repository(log: pathlib.Path) -> None:
    packages = subprocess.run(
        ["dpkg-scanpackages", "--multiversion", "."],
        cwd=_LOCAL_REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    (_LOCAL_REPO / "Packages").write_text(packages)
    run(["apt-get", "update", "-qq"], log=log)


def configure_apt() -> None:
    """Enable deb-src for every configured suite and a pinned local repository."""

    for path in pathlib.Path("/etc/apt/sources.list.d").glob("*.sources"):
        text = path.read_text()
        path.write_text(re.sub(r"(?m)^Types: deb$", "Types: deb deb-src", text))
    _LOCAL_REPO.mkdir(parents=True, exist_ok=True)
    (_LOCAL_REPO / "Packages").write_text("")
    pathlib.Path("/etc/apt/sources.list.d/dkc-patched.sources").write_text(
        "Types: deb\n"
        f"URIs: file:{_LOCAL_REPO}\n"
        "Suites: ./\n"
        "Trusted: yes\n"
    )
    pathlib.Path("/etc/apt/preferences.d/dkc-patched").write_text(
        f'Package: *\nPin: origin ""\nPin-Priority: 1001\n'
    )


def build_fix(
    fix: Fix, work: pathlib.Path, output: pathlib.Path, log: pathlib.Path
) -> list[tuple[str, ...]]:
    versions = {source.package: newest_source_version(source.package) for source in fix.sources}
    first = fix.sources[0].package
    if fix.obsolete_from_upstream_version is not None and not version_gt(
        fix.obsolete_from_upstream_version, upstream_version(versions[first])
    ):
        print(
            f"fix {fix.id}: obsolete, Debian has {first} {versions[first]}; "
            "delete its ledger block",
            flush=True,
        )
        return []
    built: dict[str, tuple[str, str, pathlib.Path]] = {}
    for source in fix.sources:
        version = versions[source.package]
        target = rebuilt_version(version, fix.revision)
        print(f"fix {fix.id}: rebuilding {source.package} {version} as {target}", flush=True)
        stage = work / source.package
        stage.mkdir(parents=True)
        run(
            ["apt-get", "source", "-qq", "--only-source", f"{source.package}={version}"],
            cwd=stage,
            log=log,
        )
        trees = [path for path in stage.iterdir() if path.is_dir()]
        if len(trees) != 1:
            raise RuntimeError(f"{source.package} unpacked into {len(trees)} trees")
        tree = trees[0]
        add_patches(tree, source.patches)
        prepend_changelog(tree, target, fix)
        run(["apt-get", "build-dep", "-y", "-qq", "./"], cwd=tree, log=log)
        # dpkg-source applies every quilt patch with zero fuzz and stops on a
        # patch that no longer matches the selected Debian source.
        run(["dpkg-buildpackage", "-b", "-us", "-uc"], cwd=tree, log=log)
        for deb in sorted(stage.glob("*.deb")):
            fields = subprocess.run(
                ["dpkg-deb", "--showformat=${Package}\t${Version}\t${Architecture}", "--show", deb],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.split("\t")
            if fields[1] != target:
                raise RuntimeError(f"{deb.name} has version {fields[1]}, expected {target}")
            shutil.copy2(deb, _LOCAL_REPO / deb.name)
            built[fields[0]] = (source.package, fields[2], _LOCAL_REPO / deb.name)
        publish_local_repository(log)
    rows: list[tuple[str, ...]] = []
    for package in fix.install:
        if package not in built:
            raise RuntimeError(f"fix {fix.id} did not build {package}")
        source_package, architecture, path = built[package]
        destination = output / "debs" / path.name
        shutil.copy2(path, destination)
        digest = subprocess.run(
            ["sha256sum", destination], check=True, capture_output=True, text=True
        ).stdout.split()[0]
        rows.append(
            (
                fix.id,
                source_package,
                versions[source_package],
                package,
                rebuilt_version(versions[source_package], fix.revision),
                architecture,
                digest,
            )
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--work", type=pathlib.Path, required=True)
    parser.add_argument(
        "--check-only", action="store_true", help="validate the ledger and exit"
    )
    args = parser.parse_args()
    ledger = load_ledger(args.ledger)
    if args.check_only:
        print(f"patched-package ledger PASS: {len(ledger.fixes)} fix(es)")
        return 0
    (args.output / "debs").mkdir(parents=True, exist_ok=True)
    args.work.mkdir(parents=True, exist_ok=True)
    log = args.work / "build.log"
    rows: list[tuple[str, ...]] = []
    if ledger.fixes:
        configure_apt()
        run(["apt-get", "update", "-qq"], log=log)
        for fix in ledger.fixes:
            rows.extend(build_fix(fix, args.work / fix.id, args.output, log))
    with (args.output / "manifest.tsv").open("w", encoding="utf-8") as stream:
        stream.write("\t".join(_MANIFEST_HEADER) + "\n")
        for row in rows:
            stream.write("\t".join(row) + "\n")
    print(f"patched packages PASS: {len(rows)} binary package(s) for the image", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
