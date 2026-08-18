#!/usr/bin/env python3
"""Reconcile the automatic release flavors into one installable package matrix."""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import pathlib
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from dkc.debver import DebianVersion
from dkc.sourcepackage import validate_source_bundle


FLAVORS = ("v2", "v3", "v4")
RELEASE_FLAVORS = ("v2", "v3")
HEX64 = re.compile(r"[0-9a-f]{64}")
RELATION_FIELDS = (
    "depends",
    "pre_depends",
    "recommends",
    "suggests",
    "enhances",
    "breaks",
    "provides",
    "conflicts",
    "replaces",
)


def fail(message: str) -> None:
    raise SystemExit(message)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def load_json(path: pathlib.Path) -> dict[str, object]:
    if not path.is_file() or path.is_symlink():
        fail(f"JSON evidence is not a plain file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read JSON evidence {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"JSON evidence is not an object: {path}")
    return value


def compare_source_reports(
    canonical: dict[str, object], candidate: dict[str, object], flavor: str
) -> dict[str, dict[str, object]]:
    """Require identical source inputs while retaining per-build upload metadata."""

    canonical_fields = {key: value for key, value in canonical.items() if key != "files"}
    candidate_fields = {key: value for key, value in candidate.items() if key != "files"}
    if candidate_fields != canonical_fields:
        fail(f"{flavor} source-package report fields differ from v2")

    metadata_names = {canonical.get("buildinfo"), canonical.get("changes")}
    if not all(isinstance(name, str) and name for name in metadata_names):
        fail("source-package report has invalid upload metadata names")

    def records(report: dict[str, object]) -> dict[str, dict[str, object]]:
        raw_records = report.get("files")
        if not isinstance(raw_records, list):
            fail(f"{flavor} source-package report has no file inventory")
        parsed: dict[str, dict[str, object]] = {}
        for raw_record in raw_records:
            if not isinstance(raw_record, dict) or set(raw_record) != {
                "name",
                "sha256",
                "size",
            }:
                fail(f"{flavor} source-package report has a malformed file record")
            name = raw_record.get("name")
            digest = raw_record.get("sha256")
            size = raw_record.get("size")
            if (
                not isinstance(name, str)
                or not name
                or name in parsed
                or not isinstance(digest, str)
                or HEX64.fullmatch(digest) is None
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                fail(f"{flavor} source-package report has an invalid file record")
            parsed[name] = raw_record
        return parsed

    canonical_records = records(canonical)
    candidate_records = records(candidate)
    if set(candidate_records) != set(canonical_records):
        fail(f"{flavor} source-package file inventory differs from v2")
    if not metadata_names.issubset(canonical_records):
        fail("source-package report omits upload metadata records")

    for name in sorted(canonical_records):
        canonical_record = canonical_records[name]
        candidate_record = candidate_records[name]
        if name in metadata_names:
            if candidate_record["size"] != canonical_record["size"]:
                fail(f"{flavor} source upload metadata size differs from v2: {name}")
        elif candidate_record != canonical_record:
            fail(f"{flavor} reproducible source member differs from v2: {name}")

    return {
        name: {
            "sha256": candidate_records[name]["sha256"],
            "size": candidate_records[name]["size"],
        }
        for name in sorted(metadata_names)
    }


def load_env(path: pathlib.Path) -> dict[str, str]:
    if not path.is_file() or path.is_symlink():
        fail(f"result evidence is not a plain file: {path}")
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(f"cannot read result evidence {path}: {exc}")
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in result:
            fail(f"malformed or duplicate result field in {path}: {line!r}")
        result[key] = value
    return result


def validate_evidence_manifest(evidence: pathlib.Path) -> None:
    manifest = evidence / "evidence.sha256"
    if not manifest.is_file() or manifest.is_symlink():
        fail(f"evidence manifest is not a plain file: {manifest}")

    records: dict[str, str] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  [.](/[^\t\r\n\0]+)", line)
        if match is None:
            fail(f"malformed evidence manifest record: {line!r}")
        relative = match.group(2).removeprefix("/")
        pure = pathlib.PurePosixPath(relative)
        if (
            not relative
            or pure.is_absolute()
            or ".." in pure.parts
            or relative == "evidence.sha256"
            or relative in records
        ):
            fail(f"unsafe or duplicate evidence manifest path: {relative!r}")
        records[relative] = match.group(1)

    expected: set[str] = set()
    for path in evidence.rglob("*"):
        if path.is_symlink():
            fail(f"evidence tree contains a symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            fail(f"evidence tree contains a special entry: {path}")
        relative = path.relative_to(evidence).as_posix()
        if relative != "evidence.sha256":
            expected.add(relative)
    if set(records) != expected:
        fail(
            "evidence manifest file set differs: "
            f"missing={sorted(expected - set(records))}, "
            f"unexpected={sorted(set(records) - expected)}"
        )
    for relative, digest in records.items():
        if sha256(evidence / relative) != digest:
            fail(f"evidence digest mismatch: {relative}")


def validate_publication_identity(
    identity: dict[str, object],
) -> tuple[str, str, dict[str, str], set[str]]:
    if identity.get("schema_version") != 1 or identity.get("source_package") != "dkc-linux":
        fail("publication identity has an unsupported schema or source package")
    digest = identity.get("build_input_digest")
    abi = identity.get("abi")
    package_version = identity.get("package_version")
    lto_mode = identity.get("lto_mode")
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        fail("publication identity has no valid build-input digest")
    if not isinstance(abi, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+~-]*", abi):
        fail("publication identity has no valid ABI")
    if not isinstance(package_version, str) or not package_version:
        fail("publication identity has no package version")
    if lto_mode not in {"none", "thin", "full"}:
        fail("publication identity has no valid LTO mode")

    raw_releases = identity.get("kernel_releases")
    if not isinstance(raw_releases, dict) or set(raw_releases) != set(FLAVORS):
        fail("publication identity kernel_releases must contain exactly v2/v3/v4")
    releases: dict[str, str] = {}
    for flavor in FLAVORS:
        release = raw_releases.get(flavor)
        expected = f"{abi}-{flavor}-amd64"
        if not isinstance(release, str) or release != expected:
            fail(f"publication identity has invalid {flavor} kernel release")
        releases[flavor] = release

    package_names = identity.get("package_names")
    if not isinstance(package_names, dict) or set(package_names) != {
        "versioned",
        "meta",
        "keyring",
    }:
        fail("publication identity has a malformed package_names object")
    versioned = package_names.get("versioned")
    meta = package_names.get("meta")
    keyring = package_names.get("keyring")
    if (
        not isinstance(versioned, list)
        or not isinstance(meta, list)
        or keyring != ["dkc-archive-keyring"]
    ):
        fail("publication package inventory is malformed")
    all_names = versioned + meta
    if not all(isinstance(name, str) and name.startswith("dkc-linux-") for name in all_names):
        fail("publication package inventory contains a foreign or non-string name")
    expected_union = set(all_names)
    if len(expected_union) != len(all_names) or len(expected_union) != 26:
        fail("publication package inventory is not an exact unique 26-package graph")

    exact_versioned = {
        f"dkc-linux-{role}-{releases[flavor]}"
        for flavor in FLAVORS
        for role in ("base", "binary", "modules", "image", "headers")
    }
    exact_versioned.update(
        {f"dkc-linux-headers-{abi}-common", f"dkc-linux-kbuild-{abi}"}
    )
    exact_meta = {
        f"dkc-linux-{role}-{flavor}-amd64"
        for flavor in FLAVORS
        for role in ("base", "image", "headers")
    }
    if set(versioned) != exact_versioned or set(meta) != exact_meta:
        fail("publication package inventory differs from the exact DKC package graph")
    return abi, package_version, releases, expected_union


def expected_for_flavor(identity: dict[str, object], flavor: str) -> set[str]:
    package_names = identity.get("package_names")
    if not isinstance(package_names, dict):
        fail("publication identity has no package_names object")
    versioned = package_names.get("versioned")
    meta = package_names.get("meta")
    if not isinstance(versioned, list) or not isinstance(meta, list):
        fail("publication package inventory is malformed")
    all_names = versioned + meta
    if not all(isinstance(name, str) for name in all_names):
        fail("publication package inventory contains a non-string name")
    suffix = f"-{flavor}-amd64"
    selected = {name for name in all_names if name.endswith(suffix)}
    abi = identity.get("abi")
    if not isinstance(abi, str):
        fail("publication identity has no ABI")
    selected.update(
        {
            f"dkc-linux-headers-{abi}-common",
            f"dkc-linux-kbuild-{abi}",
        }
    )
    expected_count = 10
    if len(selected) != expected_count:
        fail(f"{flavor} package inventory has {len(selected)} names, expected {expected_count}")
    return selected


def deb_fields(path: pathlib.Path) -> dict[str, str]:
    output = subprocess.check_output(
        [
            "dpkg-deb",
            "--showformat=${binary:Package}\t${Version}\t${source:Package}\t"
            "${source:Version}\t${Architecture}\t${Depends}\t${Pre-Depends}\t"
            "${Recommends}\t${Suggests}\t${Enhances}\t${Breaks}\t${Provides}\t"
            "${Conflicts}\t${Replaces}\n",
            "--show",
            path,
        ],
        text=True,
        encoding="utf-8",
    ).rstrip("\n")
    values = output.split("\t")
    if len(values) != 14:
        fail(f"unexpected dpkg-deb field record for {path.name}")
    return dict(
        zip(
            (
                "package",
                "version",
                "source",
                "source_version",
                "architecture",
                *RELATION_FIELDS,
            ),
            values,
            strict=True,
        )
    )


def internal_relations(value: str) -> list[tuple[str, str | None, str | None]]:
    result: list[tuple[str, str | None, str | None]] = []
    pattern = re.compile(
        r"(?<![A-Za-z0-9+.-])(dkc-linux-[a-z0-9+.-]+)"
        r"(?::[a-z0-9-]+)?(?:\s*\(([<>=]+)\s*([^)\s]+)\))?"
    )
    for match in pattern.finditer(value):
        result.append((match.group(1), match.group(2), match.group(3)))
    return result


def has_alternative_internal_relation(value: str) -> bool:
    """Return whether one comma-delimited alternative group names DKC."""

    return any("dkc-linux-" in group and "|" in group for group in value.split(","))


def expected_internal_dependencies(
    abi: str, releases: dict[str, str]
) -> dict[str, dict[str, bool]]:
    """Return package -> internal dependency -> whether exact version is required."""

    expected: dict[str, dict[str, bool]] = {
        f"dkc-linux-headers-{abi}-common": {},
        f"dkc-linux-kbuild-{abi}": {},
    }
    for flavor, krel in releases.items():
        base = f"dkc-linux-base-{krel}"
        binary = f"dkc-linux-binary-{krel}"
        modules = f"dkc-linux-modules-{krel}"
        image = f"dkc-linux-image-{krel}"
        headers = f"dkc-linux-headers-{krel}"
        base_meta = f"dkc-linux-base-{flavor}-amd64"
        image_meta = f"dkc-linux-image-{flavor}-amd64"
        headers_meta = f"dkc-linux-headers-{flavor}-amd64"
        expected.update(
            {
                base: {},
                binary: {base: True},
                modules: {base: True},
                image: {base: True, binary: True, modules: True},
                headers: {
                    base: True,
                    f"dkc-linux-headers-{abi}-common": True,
                    # The build-id-bearing ABI is part of this package name, so
                    # a version relation adds no compatibility information.
                    f"dkc-linux-kbuild-{abi}": False,
                },
                base_meta: {base: True},
                image_meta: {base_meta: True, image: True},
                headers_meta: {base_meta: True, headers: True},
            }
        )
    return expected


def expected_internal_provides(releases: dict[str, str]) -> dict[str, set[str]]:
    """Return the exact product-scoped virtual interfaces emitted by meta packages."""

    return {
        f"dkc-linux-image-{flavor}-amd64": {
            f"dkc-linux-latest-modules-{release}"
        }
        for flavor, release in releases.items()
    }


def validate_internal_dependency_graph(
    abi: str,
    package_version: str,
    releases: dict[str, str],
    package_rows: dict[str, dict[str, str]],
    selected_packages: set[str] | None = None,
) -> None:
    complete_expected = expected_internal_dependencies(abi, releases)
    if selected_packages is None:
        expected = complete_expected
    else:
        if not selected_packages <= set(complete_expected):
            fail("selected relation audit names an unknown DKC package")
        expected = {
            package: dependencies
            for package, dependencies in complete_expected.items()
            if package in selected_packages
        }
    if set(package_rows) != set(expected):
        fail("cannot validate relations for an incomplete DKC package graph")
    known = set(expected)
    expected_provides = expected_internal_provides(releases)
    for package, fields in package_rows.items():
        actual: dict[str, tuple[str, str | None, str | None]] = {}
        actual_provides: set[str] = set()
        for field in RELATION_FIELDS:
            if has_alternative_internal_relation(fields.get(field, "")):
                fail(f"{package} has an alternative internal relation in {field}")
            for related, operator, version in internal_relations(fields.get(field, "")):
                if field == "provides":
                    if operator is not None or version is not None:
                        fail(f"{package} has a versioned internal virtual Provides: {related}")
                    if related in actual_provides:
                        fail(f"{package} has duplicate internal Provides: {related}")
                    actual_provides.add(related)
                    continue
                if related not in known:
                    fail(f"{package} {field} names unknown DKC package {related}")
                if field != "depends":
                    fail(f"{package} relates to product package {related} via {field}, not Depends")
                if related in actual:
                    fail(f"{package} has duplicate internal dependency {related}")
                actual[related] = (field, operator, version)
        if set(actual) != set(expected[package]):
            fail(
                f"{package} internal dependency graph differs: "
                f"expected={sorted(expected[package])}, actual={sorted(actual)}"
            )
        for related, exact in expected[package].items():
            _field, operator, version = actual[related]
            if exact and (operator, version) != ("=", package_version):
                fail(
                    f"{package} dependency on {related} is not exactly "
                    f"{package_version}: {(operator, version)!r}"
                )
            if not exact and (operator is not None or version is not None):
                fail(f"{package} ABI-unique dependency on {related} must be unversioned")
        if actual_provides != expected_provides.get(package, set()):
            fail(
                f"{package} internal Provides differs: "
                f"expected={sorted(expected_provides.get(package, set()))}, "
                f"actual={sorted(actual_provides)}"
            )


def control_scripts(path: pathlib.Path) -> dict[str, tuple[int, str]]:
    process = subprocess.Popen(
        ["dpkg-deb", "--ctrl-tarfile", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    scripts: dict[str, tuple[int, str]] = {}
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
            for member in archive:
                name = member.name.removeprefix("./")
                if name not in {"config", "preinst", "postinst", "prerm", "postrm"}:
                    continue
                if not member.isfile() or name in scripts or member.size > 256 * 1024:
                    fail(f"unsafe or duplicate maintainer script in {path.name}: {name}")
                stream = archive.extractfile(member)
                if stream is None:
                    fail(f"cannot read maintainer script in {path.name}: {name}")
                try:
                    text = stream.read().decode("utf-8", errors="strict")
                except UnicodeDecodeError:
                    fail(f"non-UTF-8 maintainer script in {path.name}: {name}")
                scripts[name] = (member.mode & 0o7777, text)
    except BaseException:
        process.stdout.close()
        process.wait()
        raise
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    returncode = process.wait()
    if returncode:
        fail(f"dpkg-deb failed while reading control archive {path.name}: {stderr[-1000:]}")
    return scripts


def validate_maintainer_scripts(
    package: str,
    releases: dict[str, str],
    scripts: dict[str, tuple[int, str]],
) -> None:
    required: dict[str, tuple[str, ...]] = {}
    exact_krel: str | None = None
    doc_symlink_target: str | None = None
    for flavor, krel in releases.items():
        if package == f"dkc-linux-binary-{krel}":
            required = {
                "postrm": (
                    'if [ "$1" = remove ]; then',
                    "linux-run-hooks image postrm",
                ),
            }
            exact_krel = krel
            break
        if package == f"dkc-linux-image-{krel}":
            required = {
                "preinst": ("linux-run-hooks image preinst",),
                "postinst": ("linux-update-symlinks", "linux-run-hooks image postinst"),
                "prerm": ("linux-check-removal", "linux-run-hooks image prerm"),
                "postrm": ("linux-update-symlinks remove", "linux-run-hooks image postrm"),
            }
            exact_krel = krel
            break
        if package == f"dkc-linux-modules-{krel}":
            required = {"postinst": ("depmod",), "prerm": ("modules.builtin",)}
            exact_krel = krel
            break
        if package == f"dkc-linux-headers-{krel}":
            required = {"postinst": ("linux-run-hooks headers postinst",)}
            exact_krel = krel
            break
        if package == f"dkc-linux-image-{flavor}-amd64":
            required = {name: () for name in ("preinst", "postinst", "prerm", "postrm")}
            doc_symlink_target = f"dkc-linux-image-{krel}"
            break
        if package == f"dkc-linux-headers-{flavor}-amd64":
            required = {name: () for name in ("preinst", "postinst", "prerm", "postrm")}
            doc_symlink_target = f"dkc-linux-headers-{krel}"
            break
    if set(required) != set(scripts):
        fail(
            f"{package} maintainer script set differs: "
            f"expected={sorted(required)}, actual={sorted(scripts)}"
        )
    for name, needles in required.items():
        mode, text = scripts[name]
        if not mode & 0o111:
            fail(f"{package} maintainer script is not executable: {name}")
        if doc_symlink_target is not None:
            commands = [
                line
                for line in text.splitlines()
                if line and not line.startswith("#")
            ]
            if commands[:1] != ["set -e"] or len(commands) != 2:
                fail(f"{package} {name} has an unexpected maintscript command set")
            try:
                arguments = shlex.split(commands[1], posix=True)
            except ValueError:
                fail(f"{package} {name} has malformed shell quoting")
            expected_arguments = [
                "dpkg-maintscript-helper",
                "dir_to_symlink",
                f"/usr/share/doc/{package}",
                doc_symlink_target,
                "5.7~rc5-1~exp1",
                package,
                "--",
                "$@",
            ]
            if arguments != expected_arguments:
                fail(f"{package} {name} has an unexpected documentation symlink transition")
            continue
        if exact_krel is None or f"version={exact_krel}" not in text:
            fail(f"{package} {name} is not bound to an exact KREL")
        for needle in needles:
            if needle not in text:
                fail(f"{package} {name} lacks lifecycle command {needle!r}")
    if package.startswith("dkc-linux-binary-"):
        postrm = scripts["postrm"][1]
        if postrm.count("linux-run-hooks image postrm") != 1:
            fail(f"{package} does not run the removal hook exactly once")
    if package.startswith("dkc-linux-image-") and exact_krel is not None:
        postrm = scripts["postrm"][1]
        match = re.search(
            r'case "\$1" in\s*remove\|purge\)(.*?)\n\s*;;\s*\n\*\)(.*?)\n\s*;;\s*\nesac',
            postrm,
            flags=re.DOTALL,
        )
        if match is None or "linux-run-hooks image postrm" in match.group(1):
            fail(f"{package} does not defer removal hooks to the binary package")
        if "linux-run-hooks image postrm" not in match.group(2):
            fail(f"{package} no longer handles non-removal postrm actions")


def normalized_payload_member(
    member: tarfile.TarInfo, package: str
) -> tuple[str, str, int, int, str] | None:
    raw = member.name.removeprefix("./")
    if raw in ("", ".") and member.isdir():
        return None
    pure = pathlib.PurePosixPath(raw)
    if (
        not raw
        or pure.is_absolute()
        or ".." in pure.parts
        or any(character in raw for character in "\t\r\n\0")
    ):
        fail(f"unsafe package payload path in {package}: {member.name!r}")
    relative = posixpath.normpath(raw)
    if relative in ("", ".", "..") or relative.startswith("../"):
        fail(f"unsafe package payload path in {package}: {member.name!r}")
    if member.isdir():
        kind = "directory"
        target = ""
        size = 0
        if member.mode & 0o7000:
            fail(f"special permission bits in {package}: {relative}")
    elif member.isfile():
        kind = "file"
        target = ""
        size = member.size
        if member.mode & 0o7000:
            fail(f"special permission bits in {package}: {relative}")
    elif member.issym() or member.islnk():
        kind = "symlink" if member.issym() else "hardlink"
        target = member.linkname
        target_pure = pathlib.PurePosixPath(target)
        if (
            not target
            or target_pure.is_absolute()
            or any(character in target for character in "\t\r\n\0")
        ):
            fail(f"unsafe {kind} target in {package}: {relative} -> {target!r}")
        base = posixpath.dirname(relative) if member.issym() else ""
        resolved = posixpath.normpath(posixpath.join(base, target))
        if resolved in ("", ".", "..") or resolved.startswith("../"):
            fail(f"escaping {kind} target in {package}: {relative} -> {target!r}")
        size = 0
    else:
        fail(f"unsupported special payload entry in {package}: {relative}")
    return relative, kind, member.mode & 0o7777, size, target


def payload_records(path: pathlib.Path) -> list[tuple[str, str, int, int, str]]:
    process = subprocess.Popen(
        ["dpkg-deb", "--fsys-tarfile", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    result: list[tuple[str, str, int, int, str]] = []
    seen: set[str] = set()
    try:
        with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
            for member in archive:
                record = normalized_payload_member(member, path.name)
                if record is None:
                    continue
                relative = record[0]
                if relative in seen:
                    fail(f"duplicate payload path inside {path.name}: {relative}")
                seen.add(relative)
                result.append(record)
    except BaseException:
        process.stdout.close()
        process.wait()
        raise
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    returncode = process.wait()
    if returncode:
        fail(f"dpkg-deb failed while listing {path.name}: {stderr[-1000:]}")
    paths = {record[0] for record in result}
    for relative, kind, _mode, _size, target in result:
        if kind == "hardlink" and posixpath.normpath(target) not in paths:
            fail(
                f"hardlink target is not owned by {path.name}: "
                f"{relative} -> {target}"
            )
    return sorted(result)


def resolved_link(path: str, target: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(path), target))


def validate_copyright_documentation(
    package_payloads: dict[str, dict[str, tuple[str, str, int, int, str]]],
) -> None:
    packages = set(package_payloads)
    for package in sorted(packages):
        current = package
        visited: set[str] = set()
        while True:
            if current in visited:
                fail(f"documentation symlink cycle for {package}: {sorted(visited)}")
            visited.add(current)
            root = f"usr/share/doc/{current}"
            record = package_payloads[current].get(root)
            if record is None:
                fail(f"{current} does not own its documentation root {root}")
            if record[1] == "directory":
                copyright_record = package_payloads[current].get(f"{root}/copyright")
                if copyright_record is None or copyright_record[1] != "file":
                    fail(
                        f"documentation chain for {package} does not end in a "
                        "regular copyright file"
                    )
                break
            if record[1] != "symlink":
                fail(f"{current} documentation root has unexpected type {record[1]}")
            target = record[4]
            if target not in packages or "/" in target or target in (".", ".."):
                fail(
                    f"{current} documentation link does not name a packaged "
                    f"documentation root: {target}"
                )
            current = target


def validate_payload_layout(
    abi: str,
    releases: dict[str, str],
    package_payloads: dict[str, dict[str, tuple[str, str, int, int, str]]],
    payload_owners: dict[str, list[str]],
    *,
    btf_required: bool,
) -> None:
    def require(package: str, path: str, kind: str = "file") -> tuple[str, str, int, int, str]:
        record = package_payloads.get(package, {}).get(path)
        if record is None or record[1] != kind:
            fail(f"{package} does not own required {kind} payload {path}")
        if payload_owners.get(path) != [package]:
            fail(f"required payload has an ambiguous owner: {path}")
        return record

    if any(path.startswith("usr/src/dkc-linux-headers-") for path in payload_owners):
        fail("renamed package name leaked into the conventional /usr/src header ABI")

    common = f"dkc-linux-headers-{abi}-common"
    kbuild = f"dkc-linux-kbuild-{abi}"
    if any(
        path.startswith(("usr/lib/dkc-linux-kbuild-", "usr/src/dkc-linux-kbuild-"))
        for path in payload_owners
    ):
        fail("renamed package name leaked into the conventional Kbuild filesystem ABI")
    require(common, f"usr/src/linux-headers-{abi}-common/Makefile")
    for leaf in ("scripts", "tools"):
        path = f"usr/src/linux-headers-{abi}-common/{leaf}"
        record = require(common, path, "symlink")
        expected = f"usr/lib/linux-kbuild-{abi}/{leaf}"
        if resolved_link(path, record[4]) != expected:
            fail(f"common header {leaf} link does not resolve to {expected}")
        if not any(item.startswith(expected + "/") for item in package_payloads[kbuild]):
            fail(f"{kbuild} has no payload below {expected}")
    kbuild_source = f"usr/src/linux-kbuild-{abi}"
    kbuild_source_record = require(kbuild, kbuild_source, "symlink")
    if resolved_link(kbuild_source, kbuild_source_record[4]) != f"usr/lib/linux-kbuild-{abi}":
        fail(f"{kbuild} compatibility link does not resolve to its conventional payload")

    module_roots: set[str] = set()
    for path in payload_owners:
        match = re.match(r"usr/lib/modules/([^/]+)/", path)
        if match:
            module_roots.add(match.group(1))
    if module_roots != set(releases.values()):
        fail(f"package payload module roots differ from the release KRELs: {sorted(module_roots)}")

    for flavor, krel in releases.items():
        base = f"dkc-linux-base-{krel}"
        binary = f"dkc-linux-binary-{krel}"
        modules = f"dkc-linux-modules-{krel}"
        headers = f"dkc-linux-headers-{krel}"
        require(base, f"boot/config-{krel}")
        require(base, f"boot/System.map-{krel}")
        require(binary, f"boot/vmlinuz-{krel}")
        module_prefix = f"usr/lib/modules/{krel}/kernel/"
        if not any(
            path.startswith(module_prefix) and re.search(r"[.]ko(?:[.](?:xz|zst))?$", path)
            for path in package_payloads[modules]
        ):
            fail(f"{modules} contains no module below {module_prefix}")

        header_root = f"usr/src/linux-headers-{krel}"
        for leaf in (".kernelvariables", "Makefile", "Module.symvers"):
            require(headers, f"{header_root}/{leaf}")
        header_vmlinux = f"{header_root}/vmlinux"
        if btf_required:
            require(headers, header_vmlinux)
        elif header_vmlinux in package_payloads[headers]:
            fail(f"{headers} contains a vmlinux payload while BTF is disabled")
        for leaf, expected in (
            ("build", header_root),
            ("source", f"usr/src/linux-headers-{abi}-common"),
        ):
            path = f"usr/lib/modules/{krel}/{leaf}"
            record = require(headers, path, "symlink")
            if resolved_link(path, record[4]) != expected:
                fail(f"{headers} {leaf} link does not resolve to {expected}")

    # Keep package roles stable across Debian generator changes. Documentation,
    # bug metadata, and Lintian overrides may evolve below /usr/share, but every
    # boot, module, header, and Kbuild payload must stay in its exact package and
    # identity-scoped tree.
    allowed: dict[str, tuple[str, ...]] = {
        common: ("usr/share/", f"usr/src/linux-headers-{abi}-common/"),
        kbuild: (
            "usr/share/",
            f"usr/lib/linux-kbuild-{abi}/",
            f"usr/src/linux-kbuild-{abi}",
        ),
    }
    for flavor, krel in releases.items():
        allowed.update(
            {
                f"dkc-linux-base-{krel}": (
                    "usr/share/",
                    f"boot/config-{krel}",
                    f"boot/System.map-{krel}",
                    f"usr/lib/modules/{krel}/modules.builtin",
                    f"usr/lib/modules/{krel}/modules.builtin.modinfo",
                    f"usr/lib/modules/{krel}/modules.order",
                ),
                f"dkc-linux-binary-{krel}": (
                    "usr/share/",
                    f"boot/vmlinuz-{krel}",
                ),
                f"dkc-linux-modules-{krel}": (
                    "usr/share/",
                    f"usr/lib/modules/{krel}/kernel/",
                ),
                f"dkc-linux-image-{krel}": ("usr/share/",),
                f"dkc-linux-headers-{krel}": (
                    "usr/share/",
                    f"usr/src/linux-headers-{krel}/",
                    f"usr/lib/modules/{krel}/build",
                    f"usr/lib/modules/{krel}/source",
                ),
                f"dkc-linux-base-{flavor}-amd64": ("usr/share/",),
                f"dkc-linux-image-{flavor}-amd64": ("usr/share/",),
                f"dkc-linux-headers-{flavor}-amd64": ("usr/share/",),
            }
        )
    if set(allowed) != set(package_payloads):
        fail("cannot validate payload ownership for an incomplete package graph")
    for package, records in package_payloads.items():
        for path, record in records.items():
            if record[1] == "directory":
                accepted = any(
                    prefix.rstrip("/") == path
                    or prefix.rstrip("/").startswith(path + "/")
                    or (prefix.endswith("/") and path.startswith(prefix))
                    for prefix in allowed[package]
                )
            else:
                accepted = any(
                    path == prefix or (prefix.endswith("/") and path.startswith(prefix))
                    for prefix in allowed[package]
                )
            if not accepted:
                fail(f"{package} owns an unreviewed payload path: {path}")


def audit_one_flavor(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("flavor", choices=FLAVORS)
    parser.add_argument("root", type=pathlib.Path)
    parser.add_argument("repository", type=pathlib.Path)
    parser.add_argument("report", type=pathlib.Path)
    args = parser.parse_args(arguments)

    if args.repository.exists() and any(args.repository.iterdir()):
        fail(f"refusing non-empty package directory: {args.repository}")
    args.repository.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    evidence = args.root / "evidence"
    artifacts = args.root / "artifacts"
    if (
        not evidence.is_dir()
        or evidence.is_symlink()
        or not artifacts.is_dir()
        or artifacts.is_symlink()
    ):
        fail(f"{args.flavor} export lacks plain evidence/artifact directories")
    result = load_env(evidence / "result.env")
    if result.get("status") != "PASS" or result.get("flavor") != args.flavor:
        fail(f"{args.flavor} result is not an accepted export: {result}")

    identity = load_json(evidence / "publication-identity.json")
    abi, package_version, releases, _expected_union = validate_publication_identity(identity)
    lto_mode = str(identity["lto_mode"])
    btf_policy = "required" if lto_mode == "none" else "forbidden"
    attestation = load_json(evidence / "attestation.json")
    if (
        attestation.get("status") != "PASS"
        or attestation.get("flavor") != args.flavor
        or attestation.get("kernel_release") != releases[args.flavor]
        or attestation.get("lto_mode") != lto_mode
        or attestation.get("btf_policy") != btf_policy
    ):
        fail(f"{args.flavor} attestation identity does not match the publication")
    attested_packages = attestation.get("packages")
    if not isinstance(attested_packages, dict) or not all(
        isinstance(name, str)
        and isinstance(digest, str)
        and HEX64.fullmatch(digest)
        for name, digest in attested_packages.items()
    ):
        fail(f"{args.flavor} attestation has a malformed package digest map")

    expected = expected_for_flavor(identity, args.flavor)
    found: set[str] = set()
    package_files: dict[str, str] = {}
    package_hashes: dict[str, str] = {}
    package_rows: dict[str, dict[str, str]] = {}
    payload_owners: dict[str, list[str]] = defaultdict(list)
    debs = sorted(artifacts.glob("*.deb"))
    if any(artifacts.glob("*.ddeb")) or any(artifacts.glob("*.udeb")):
        fail(f"{args.flavor} export contains unpublished binary artifacts")
    for deb in debs:
        if not deb.is_file() or deb.is_symlink():
            fail(f"{args.flavor} artifact is not a plain file: {deb.name}")
        fields = deb_fields(deb)
        package = fields["package"]
        if package in found:
            fail(f"duplicate package in {args.flavor} export: {package}")
        if package not in expected:
            fail(f"unexpected package in {args.flavor} export: {package}")
        if (
            fields["version"] != package_version
            or fields["source"] != "dkc-linux"
            or fields["source_version"] != package_version
        ):
            fail(f"{package} carries a foreign source/version identity")
        expected_architecture = (
            "all" if package == f"dkc-linux-headers-{abi}-common" else "amd64"
        )
        if fields["architecture"] != expected_architecture:
            fail(f"{package} has unexpected architecture {fields['architecture']!r}")
        if fields["conflicts"] or fields["replaces"]:
            fail(f"{package} declares an unreviewed Conflicts/Replaces relation")
        digest = sha256(deb)
        if attested_packages.get(deb.name) != digest:
            fail(f"{deb.name} digest differs from its flavor attestation")
        records = payload_records(deb)
        for path, kind, _mode, _size, _target in records:
            if kind != "directory":
                payload_owners[path].append(package)
        validate_maintainer_scripts(package, releases, control_scripts(deb))
        target = args.repository / deb.name
        if target.exists():
            fail(f"duplicate repository filename: {deb.name}")
        shutil.copyfile(deb, target)
        if sha256(target) != digest:
            fail(f"repository copy changed {deb.name}")
        package_files[package] = deb.name
        package_hashes[package] = digest
        package_rows[package] = fields
        found.add(package)

    if set(attested_packages) != {path.name for path in debs}:
        fail(f"{args.flavor} attestation and artifact .deb file sets differ")
    if found != expected or len(found) != 10:
        fail(
            f"{args.flavor} package set differs from its exact ten-package export: "
            f"missing={sorted(expected - found)}, unexpected={sorted(found - expected)}"
        )
    validate_evidence_manifest(evidence)
    validate_internal_dependency_graph(
        abi, package_version, releases, package_rows, selected_packages=expected
    )
    collisions = {
        path: owners for path, owners in payload_owners.items() if len(owners) > 1
    }
    if collisions:
        sample = dict(list(sorted(collisions.items()))[:10])
        fail(f"package payload paths collide inside {args.flavor}: {sample}")

    report = {
        "schema_version": 1,
        "status": "PASS",
        "scope": "single-flavor-vm-input",
        "flavor": args.flavor,
        "abi": abi,
        "kernel_release": releases[args.flavor],
        "package_version": package_version,
        "lto_mode": lto_mode,
        "btf_policy": btf_policy,
        "package_count": len(found),
        "payload_collision_count": 0,
        "internal_dependency_graph": "PASS",
        "packages": package_files,
        "package_sha256": package_hashes,
        "install_method": "direct-dpkg",
    }
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{args.flavor} VM input audit PASS: {len(found)} packages")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    for flavor in RELEASE_FLAVORS:
        parser.add_argument(flavor, type=pathlib.Path)
    parser.add_argument("repository", type=pathlib.Path)
    parser.add_argument("report", type=pathlib.Path)
    args = parser.parse_args()
    roots = {flavor: getattr(args, flavor) for flavor in RELEASE_FLAVORS}

    if args.repository.exists() and any(args.repository.iterdir()):
        fail(f"refusing non-empty package repository: {args.repository}")
    args.repository.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)

    identity_bytes: bytes | None = None
    identity: dict[str, object] | None = None
    package_version: str | None = None
    abi: str | None = None
    releases: dict[str, str] | None = None
    expected_union: set[str] | None = None
    expected_release_union: set[str] | None = None
    lto_mode: str | None = None
    all_packages: dict[str, pathlib.Path] = {}
    package_rows: dict[str, dict[str, str]] = {}
    package_payloads: dict[str, dict[str, tuple[str, str, int, int, str]]] = {}
    package_maintainer_scripts: dict[str, list[str]] = {}
    payload_owners: dict[str, list[str]] = defaultdict(list)
    per_flavor: dict[str, list[str]] = {}
    common_packages: set[str] = set()
    common_copies: dict[str, dict[str, dict[str, object]]] = defaultdict(dict)
    source_report: dict[str, object] | None = None
    source_report_sha256: dict[str, str] = {}
    source_upload_metadata: dict[str, dict[str, dict[str, object]]] = {}
    source_bundle_paths: list[pathlib.Path] = []
    input_package_count = 0

    for flavor, root in roots.items():
        evidence = root / "evidence"
        artifacts = root / "artifacts"
        if (
            not evidence.is_dir()
            or evidence.is_symlink()
            or not artifacts.is_dir()
            or artifacts.is_symlink()
        ):
            fail(f"{flavor} export lacks plain evidence/artifact directories")
        result = load_env(evidence / "result.env")
        if result.get("status") != "PASS" or result.get("flavor") != flavor:
            fail(f"{flavor} result is not an accepted export: {result}")

        current_identity_bytes = (evidence / "publication-identity.json").read_bytes()
        if identity_bytes is None:
            identity_bytes = current_identity_bytes
            identity = load_json(evidence / "publication-identity.json")
            abi, package_version, releases, expected_union = validate_publication_identity(identity)
            lto_mode = str(identity["lto_mode"])
            expected_release_union = set().union(
                *(expected_for_flavor(identity, selected) for selected in RELEASE_FLAVORS)
            )
            common_packages = {
                f"dkc-linux-headers-{abi}-common",
                f"dkc-linux-kbuild-{abi}",
            }
        elif current_identity_bytes != identity_bytes:
            fail(f"{flavor} publication identity bytes differ from v2")
        assert identity is not None and package_version is not None and releases is not None

        source_report_path = evidence / "source-package/source-package.json"
        current_source_report_bytes = source_report_path.read_bytes()
        current_source_report = load_json(source_report_path)
        source_report_sha256[flavor] = hashlib.sha256(
            current_source_report_bytes
        ).hexdigest()
        if source_report is None:
            source_report = current_source_report
        source_upload_metadata[flavor] = compare_source_reports(
            source_report, current_source_report, flavor
        )
        if (
            current_source_report.get("status") != "PASS"
            or current_source_report.get("reconstruction") != "PASS"
            or current_source_report.get("build_input_digest")
            != identity.get("build_input_digest")
            or current_source_report.get("version") != package_version
        ):
            fail(f"{flavor} source-package report is not accepted")
        source_root = root / "source"
        if not source_root.is_dir() or source_root.is_symlink():
            fail(f"{flavor} export lacks a plain source bundle directory")
        source_bundle = validate_source_bundle(
            source_root,
            package="dkc-linux",
            version=package_version,
            upstream_version=DebianVersion.parse(
                str(identity.get("debian_source_version"))
            ).upstream_release,
            expected_binary_packages=expected_union or set(),
        )
        if source_bundle.to_dict() != {
            key: current_source_report[key]
            for key in source_bundle.to_dict()
        }:
            fail(f"{flavor} source bundle differs from its source-package report")
        if flavor == "v2":
            source_bundle_paths = [
                source_root / item.name for item in source_bundle.files
            ]

        attestation = load_json(evidence / "attestation.json")
        if (
            attestation.get("status") != "PASS"
            or attestation.get("flavor") != flavor
            or attestation.get("kernel_release") != releases[flavor]
            or attestation.get("lto_mode") != lto_mode
            or attestation.get("btf_policy")
            != ("required" if lto_mode == "none" else "forbidden")
        ):
            fail(f"{flavor} attestation identity does not match the publication")
        attested_packages = attestation.get("packages")
        if not isinstance(attested_packages, dict):
            fail(f"{flavor} attestation has no package digest map")
        if not all(
            isinstance(name, str)
            and isinstance(digest, str)
            and HEX64.fullmatch(digest)
            for name, digest in attested_packages.items()
        ):
            fail(f"{flavor} attestation has a malformed package digest map")

        expected = expected_for_flavor(identity, flavor)
        found: set[str] = set()
        debs = sorted(artifacts.glob("*.deb"))
        non_release_binaries = sorted(
            path.name
            for pattern in ("*.ddeb", "*.udeb")
            for path in artifacts.glob(pattern)
        )
        if non_release_binaries:
            fail(f"{flavor} export contains unpublished binary artifacts: {non_release_binaries}")
        for deb in debs:
            input_package_count += 1
            if not deb.is_file() or deb.is_symlink():
                fail(f"{flavor} artifact is not a plain file: {deb.name}")
            fields = deb_fields(deb)
            package = fields["package"]
            if fields["version"] != package_version or fields["source"] != "dkc-linux":
                fail(f"{package} carries a foreign source/version identity")
            if fields["source_version"] != package_version:
                fail(f"{package} source version differs from its binary version")
            expected_architecture = "all" if package == f"dkc-linux-headers-{abi}-common" else "amd64"
            if fields["architecture"] != expected_architecture:
                fail(
                    f"{package} architecture {fields['architecture']!r} "
                    f"!= {expected_architecture!r}"
                )
            if fields["conflicts"] or fields["replaces"]:
                fail(f"{package} declares an unreviewed Conflicts/Replaces relation")
            digest = sha256(deb)
            if attested_packages.get(deb.name) != digest:
                fail(f"{deb.name} digest differs from its flavor attestation")
            if package in all_packages:
                if package not in common_packages:
                    fail(f"flavor-specific package is duplicated across jobs: {package}")
                canonical = all_packages[package]
                if flavor == "v2":
                    fail(f"canonical common package is duplicated inside v2: {package}")
                if (
                    deb.name != canonical.name
                    or deb.stat().st_size != canonical.stat().st_size
                    or digest != sha256(canonical)
                    or fields != package_rows[package]
                ):
                    fail(
                        f"common package copy differs from canonical v2 bytes: "
                        f"{package} in {flavor}"
                    )
                common_copies[package][flavor] = {
                    "filename": deb.name,
                    "size": deb.stat().st_size,
                    "sha256": digest,
                }
                found.add(package)
                continue
            records = payload_records(deb)
            for path, kind, _mode, _size, _target in records:
                if kind != "directory":
                    payload_owners[path].append(package)
            package_payloads[package] = {record[0]: record for record in records}
            scripts = control_scripts(deb)
            validate_maintainer_scripts(package, releases, scripts)
            package_maintainer_scripts[package] = sorted(scripts)
            package_rows[package] = fields
            all_packages[package] = deb
            if package in common_packages:
                if flavor != "v2":
                    fail(f"common package lacks its canonical v2 copy: {package}")
                common_copies[package][flavor] = {
                    "filename": deb.name,
                    "size": deb.stat().st_size,
                    "sha256": digest,
                }
            found.add(package)

        if set(attested_packages) != {path.name for path in debs}:
            fail(f"{flavor} attestation and artifact .deb file sets differ")
        if found != expected:
            fail(
                f"{flavor} package set differs from the publication inventory: "
                f"missing={sorted(expected - found)}, unexpected={sorted(found - expected)}"
            )
        validate_evidence_manifest(evidence)
        per_flavor[flavor] = sorted(found)

    assert (
        identity is not None
        and identity_bytes is not None
        and package_version is not None
        and abi is not None
        and releases is not None
        and expected_union is not None
        and expected_release_union is not None
        and lto_mode is not None
        and source_report is not None
    )
    if set(all_packages) != expected_release_union or len(all_packages) != 18:
        fail("release flavor exports do not form the exact 18-package distribution graph")
    if input_package_count != 20:
        fail(f"release flavor exports contain {input_package_count} packages, expected 20")
    if set(common_copies) != common_packages or any(
        set(copies) != set(RELEASE_FLAVORS) for copies in common_copies.values()
    ):
        fail("common packages do not have one verified copy in every release flavor export")
    validate_internal_dependency_graph(
        abi,
        package_version,
        releases,
        package_rows,
        selected_packages=expected_release_union,
    )

    collisions = {
        path: owners for path, owners in payload_owners.items() if len(owners) > 1
    }
    if collisions:
        sample = dict(list(sorted(collisions.items()))[:10])
        fail(f"package payload paths collide across the matrix: {sample}")
    release_releases = {flavor: releases[flavor] for flavor in RELEASE_FLAVORS}
    validate_payload_layout(
        abi,
        release_releases,
        package_payloads,
        payload_owners,
        btf_required=lto_mode == "none",
    )
    validate_copyright_documentation(package_payloads)

    payload_inventory = args.report.parent / "package-payloads.tsv.xz"
    with lzma.open(payload_inventory, "wt", encoding="utf-8", preset=1) as output:
        output.write("package\ttype\tmode\tsize\tpath\tlink_target\n")
        for package, records in sorted(package_payloads.items()):
            for path, kind, mode, size, target in sorted(records.values()):
                output.write(
                    f"{package}\t{kind}\t{mode:04o}\t{size}\t{path}\t{target}\n"
                )
    (args.report.parent / "publication-identity.json").write_bytes(identity_bytes)

    repository_files: dict[str, str] = {}
    for package, source in sorted(all_packages.items()):
        target = args.repository / source.name
        if target.exists():
            fail(f"duplicate repository filename: {source.name}")
        shutil.copyfile(source, target)
        if sha256(target) != sha256(source):
            fail(f"repository copy changed {source.name}")
        repository_files[package] = source.name

    source_repository_files: dict[str, str] = {}
    if len(source_bundle_paths) != 5:
        fail("canonical source bundle is incomplete")
    for source in sorted(source_bundle_paths):
        target = args.repository / source.name
        if target.exists():
            fail(f"source filename collides in the repository: {source.name}")
        shutil.copyfile(source, target)
        if sha256(target) != sha256(source):
            fail(f"repository copy changed source member {source.name}")
        source_repository_files[source.name] = sha256(target)

    packages_index = args.repository / "Packages"
    with packages_index.open("wb") as output:
        scan = subprocess.run(
            ["dpkg-scanpackages", ".", "/dev/null"],
            cwd=args.repository,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if scan.returncode:
        fail(f"dpkg-scanpackages failed: {scan.stderr.decode(errors='replace')[-1000:]}")

    sources_index = args.repository / "Sources"
    with sources_index.open("wb") as output:
        scan_sources = subprocess.run(
            ["dpkg-scansources", ".", "/dev/null"],
            cwd=args.repository,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
        )
    if scan_sources.returncode:
        fail(
            "dpkg-scansources failed: "
            f"{scan_sources.stderr.decode(errors='replace')[-1000:]}"
        )
    sources_text = sources_index.read_text(encoding="utf-8")
    if sources_text.count("Package: dkc-linux\n") != 1:
        fail("Sources does not contain exactly one dkc-linux stanza")

    report = {
        "schema_version": 1,
        "status": "PASS",
        "publishable": False,
        "scope": "current-package-matrix",
        "upgrade_between_dkc_revisions": "NOT_RUN",
        "build_input_digest": identity.get("build_input_digest"),
        "package_version": package_version,
        "abi": abi,
        "lto_mode": lto_mode,
        "btf_policy": "required" if lto_mode == "none" else "forbidden",
        "kernel_releases": releases,
        "release_flavors": list(RELEASE_FLAVORS),
        "input_package_count": input_package_count,
        "package_count": len(all_packages),
        "common_package_canonical_flavor": "v2",
        "common_package_copies": common_copies,
        "payload_path_count": len(payload_owners),
        "payload_collisions": 0,
        "payload_layout": "PASS",
        "copyright_documentation": "PASS",
        "protected_payload_ownership": "PASS",
        "internal_dependency_graph": "PASS",
        "maintainer_script_lifecycle": "PASS",
        "payload_inventory": payload_inventory.name,
        "payload_inventory_sha256": sha256(payload_inventory),
        "publication_identity_sha256": sha256(
            args.report.parent / "publication-identity.json"
        ),
        "per_flavor_packages": per_flavor,
        "repository_packages": repository_files,
        "repository_packages_sha256": sha256(packages_index),
        "source_bundle": source_report,
        "source_report_sha256": source_report_sha256,
        "source_repository_files": source_repository_files,
        "source_upload_metadata": source_upload_metadata,
        "sources_index_sha256": sha256(sources_index),
        "package_fields": package_rows,
        "package_maintainer_scripts": package_maintainer_scripts,
    }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        f"package matrix PASS: {len(all_packages)} packages, "
        f"{len(payload_owners)} unique non-directory payload paths"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--flavor":
        raise SystemExit(audit_one_flavor(sys.argv[2:]))
    raise SystemExit(main())
