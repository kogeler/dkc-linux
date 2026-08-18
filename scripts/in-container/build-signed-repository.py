#!/usr/bin/env python3
"""Assemble, sign, and verify the complete binary/source APT repository."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import lzma
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import cast


SUPPORTED_FLAVORS = ("v2", "v3", "v4")
RELEASE_FLAVORS = ("v2", "v3")
INDEX_PATHS = (
    "dists/trixie/main/binary-amd64/Packages",
    "dists/trixie/main/binary-amd64/Packages.gz",
    "dists/trixie/main/binary-amd64/Packages.xz",
    "dists/trixie/main/source/Sources",
    "dists/trixie/main/source/Sources.gz",
    "dists/trixie/main/source/Sources.xz",
)
VALIDITY_SECONDS = 14 * 86400
HEX40 = re.compile(r"^[0-9A-F]{40}$")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def signed_publication_ids(
    *,
    stamp: str,
    generation: int,
    request: bytes,
    inrelease: bytes,
    release_signature: bytes,
) -> tuple[str, str]:
    """Derive immutable namespaces from every byte that shapes the manifest.

    A date plus build ID is not sufficient: a retry in the same UTC day can
    carry a new Release date and signatures while targeting the same
    generation.  Content-bound IDs let that retry coexist with immutable
    objects left by an interrupted attempt.
    """

    if not re.fullmatch(r"[0-9]{8}", stamp) or generation < 0:
        raise ValueError("publication identity inputs are invalid")
    digest = hashlib.sha256(b"dkc-signed-publication-v1\0")
    for payload in (request, inrelease, release_signature):
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    suffix = digest.hexdigest()[:16]
    return (
        f"{stamp}-p{suffix}-g{generation}",
        f"{stamp}-t{suffix}-g{generation}",
    )


def copy_exact(source: pathlib.Path, target: pathlib.Path) -> None:
    if not source.is_file() or source.is_symlink() or target.exists():
        raise ValueError(f"unsafe or duplicate publication input: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(source.stat().st_mode & 0o777)
    if sha256(source) != sha256(target):
        raise ValueError(f"publication copy changed {source.name}")


def copy_or_reuse_exact(source: pathlib.Path, target: pathlib.Path) -> None:
    if target.exists():
        if (
            not source.is_file()
            or source.is_symlink()
            or not target.is_file()
            or target.is_symlink()
            or source.stat().st_size != target.stat().st_size
            or sha256(source) != sha256(target)
        ):
            raise ValueError(f"immutable pool collision: {target.name}")
        return
    copy_exact(source, target)


def parse_control_records(path: pathlib.Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for paragraph in path.read_text(encoding="utf-8").strip().split("\n\n"):
        record: dict[str, str] = {}
        for line in paragraph.splitlines():
            if not line[:1].isspace() and ": " in line:
                name, value = line.split(": ", 1)
                record[name] = value
        if record:
            records.append(record)
    return records


def pool_version(path: pathlib.Path):
    from dkc.debver import DebianVersion

    name = path.name
    if name.endswith(".deb"):
        version = subprocess.check_output(
            ["dpkg-deb", "--showformat=${Version}", "--show", path],
            text=True,
            encoding="utf-8",
        )
    elif name.endswith(".dsc"):
        match = re.search(r"^Version:\s*(\S+)\s*$", path.read_text(encoding="utf-8"), re.MULTILINE)
        if match is None:
            raise ValueError(f"source descriptor lacks a Version: {name}")
        version = match.group(1)
    elif name.endswith(".orig.tar.xz"):
        version = name.split("_", 1)[1].removesuffix(".orig.tar.xz")
    elif name.endswith(".debian.tar.xz"):
        version = name.split("_", 1)[1].removesuffix(".debian.tar.xz")
    else:
        raise ValueError(f"unclassified kernel pool object: {name}")
    return DebianVersion.parse(version)


def apply_retention(
    binary_pool: pathlib.Path,
    *,
    mode: str,
    max_bytes: int | None,
    fixed_bytes: int,
):
    """Apply the typed, patch-release-atomic pool policy."""
    from dkc.retention import PoolObject, select_retained_objects

    paths = [path for path in sorted(binary_pool.iterdir()) if path.is_file()]
    decision = select_retained_objects(
        (
            PoolObject(path.name, pool_version(path), path.stat().st_size)
            for path in paths
        ),
        mode=mode,
        max_bytes=max_bytes,
        fixed_bytes=fixed_bytes,
    )
    for path in paths:
        if path.name not in decision.retained_keys:
            path.unlink()
    return decision


def release_package_inventory(
    identity: dict[str, object],
) -> tuple[list[str], list[str], list[str]]:
    """Validate the supported source graph and select the binary release subset."""

    names = identity.get("package_names")
    abi = identity.get("abi")
    releases = identity.get("kernel_releases")
    if not isinstance(names, dict):
        raise ValueError("publication identity lacks package names")
    versioned = names.get("versioned")
    meta = names.get("meta")
    if not isinstance(versioned, list) or not isinstance(meta, list):
        raise ValueError("publication identity has a malformed package inventory")
    if not isinstance(abi, str) or not abi:
        raise ValueError("publication identity lacks an ABI")
    if not isinstance(releases, dict) or set(releases) != set(SUPPORTED_FLAVORS):
        raise ValueError("publication identity has an invalid supported flavor set")
    if any(
        releases.get(flavor) != f"{abi}-{flavor}-amd64"
        for flavor in SUPPORTED_FLAVORS
    ):
        raise ValueError("publication identity has an invalid kernel release")

    source_binaries = versioned + meta
    if not all(isinstance(item, str) for item in source_binaries):
        raise ValueError("publication identity contains a non-string package name")
    common = {f"dkc-linux-headers-{abi}-common", f"dkc-linux-kbuild-{abi}"}
    expected_source = set(common)
    expected_source.update(
        f"dkc-linux-{role}-{releases[flavor]}"
        for flavor in SUPPORTED_FLAVORS
        for role in ("base", "binary", "modules", "image", "headers")
    )
    expected_source.update(
        f"dkc-linux-{role}-{flavor}-amd64"
        for flavor in SUPPORTED_FLAVORS
        for role in ("base", "image", "headers")
    )
    if len(source_binaries) != 26 or set(source_binaries) != expected_source:
        raise ValueError("publication identity does not contain the exact source package graph")

    expected_release = set(common)
    expected_release.update(
        f"dkc-linux-{role}-{releases[flavor]}"
        for flavor in RELEASE_FLAVORS
        for role in ("base", "binary", "modules", "image", "headers")
    )
    release_meta = [
        f"dkc-linux-{role}-{flavor}-amd64"
        for role in ("base", "image", "headers")
        for flavor in RELEASE_FLAVORS
    ]
    expected_release.update(release_meta)
    binaries = [item for item in source_binaries if item in expected_release]
    if len(binaries) != 18 or set(binaries) != expected_release:
        raise ValueError("publication identity does not yield the exact release package graph")
    return source_binaries, binaries, release_meta


def run_to_file(command: list[str], output: pathlib.Path, cwd: pathlib.Path) -> None:
    with output.open("wb") as stream:
        result = subprocess.run(
            command,
            cwd=cwd,
            stdout=stream,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode:
        tail = result.stderr.decode(errors="replace")[-2000:]
        raise RuntimeError(f"{' '.join(command)} failed: {tail}")


def compress_index(path: pathlib.Path) -> None:
    payload = path.read_bytes()
    with (path.parent / f"{path.name}.gz").open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as stream:
            stream.write(payload)
    with lzma.open(
        path.parent / f"{path.name}.xz",
        "wb",
        format=lzma.FORMAT_XZ,
        check=lzma.CHECK_SHA256,
        preset=9,
    ) as stream:
        stream.write(payload)


def load_fingerprints(path: pathlib.Path) -> list[str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"fingerprint input is not a plain file: {path}")
    values = path.read_text(encoding="ascii").splitlines()
    if (
        not values
        or len(values) != len(set(values))
        or not all(HEX40.fullmatch(value) for value in values)
    ):
        raise ValueError(f"fingerprint input is malformed: {path}")
    return values


def public_key_inventory(
    path: pathlib.Path, gpg_home: pathlib.Path
) -> tuple[str, dict[str, tuple[int, int]]]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("public archive keyring is not a plain file")
    output = subprocess.check_output(
        [
            "gpg",
            "--homedir",
            str(gpg_home),
            "--batch",
            "--show-keys",
            "--with-colons",
            str(path),
        ],
        text=True,
        encoding="utf-8",
    )
    primary: str | None = None
    subkeys: dict[str, tuple[int, int]] = {}
    pending: tuple[str, list[str]] | None = None
    for line in output.splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            if primary is not None or pending is not None:
                raise ValueError("public archive bundle contains more than one primary key")
            if fields[1] in {"d", "e", "i", "r"} or "c" not in fields[11].lower():
                raise ValueError("archive primary key is unusable or cannot certify")
            pending = ("pub", fields)
        elif fields[0] == "sub":
            if fields[1] in {"d", "e", "i", "r"} or "s" not in fields[11].lower():
                raise ValueError("archive bundle contains a non-signing or unusable subkey")
            pending = ("sub", fields)
        elif fields[0] == "fpr" and pending is not None:
            kind, key_fields = pending
            fingerprint = fields[9]
            if not HEX40.fullmatch(fingerprint):
                raise ValueError("GnuPG returned a malformed archive fingerprint")
            if not key_fields[5].isdecimal() or not key_fields[6].isdecimal():
                raise ValueError("archive keys must have finite creation and expiry times")
            if kind == "pub":
                primary = fingerprint
            else:
                if fingerprint in subkeys:
                    raise ValueError("archive bundle contains a duplicate signing subkey")
                subkeys[fingerprint] = (int(key_fields[5]), int(key_fields[6]))
            pending = None
    if primary is None or not subkeys:
        raise ValueError("archive bundle must contain one primary and signing subkeys")
    return primary, subkeys


def validate_public_material(
    keyring: pathlib.Path,
    primary_path: pathlib.Path,
    subkeys_path: pathlib.Path,
    gpg_home: pathlib.Path,
) -> tuple[str, list[str], dict[str, tuple[int, int]]]:
    primary_values = load_fingerprints(primary_path)
    if len(primary_values) != 1:
        raise ValueError("archive primary fingerprint file must contain exactly one line")
    signing_subkeys = load_fingerprints(subkeys_path)
    primary, inventory = public_key_inventory(keyring, gpg_home)
    if primary_values[0] != primary:
        raise ValueError("public archive key does not match the tracked primary fingerprint")
    if set(signing_subkeys) != set(inventory):
        raise ValueError("public archive key does not match the tracked signing subkeys")
    return primary, signing_subkeys, inventory


def check_secret_subkey(
    gpg_home: pathlib.Path,
    *,
    primary_fingerprint: str,
    active_fingerprint: str,
    valid_until_epoch: int,
    clock_skew_seconds: int,
    safety_seconds: int,
) -> dict[str, object]:
    output = subprocess.check_output(
        [
            "gpg",
            "--homedir",
            str(gpg_home),
            "--batch",
            "--with-colons",
            "--with-keygrip",
            "--list-secret-keys",
        ],
        text=True,
        encoding="utf-8",
    )
    current: tuple[str, list[str]] | None = None
    records: list[tuple[str, str, list[str]]] = []
    for line in output.splitlines():
        fields = line.split(":")
        if fields[0] in {"sec", "ssb"}:
            current = (fields[0], fields)
        elif fields[0] == "fpr" and current is not None:
            records.append((current[0], fields[9], current[1]))
            current = None
    primary_records = [record for record in records if record[0] == "sec"]
    subkey_records = [record for record in records if record[0] == "ssb"]
    if len(primary_records) != 1 or primary_records[0][1] != primary_fingerprint:
        raise ValueError("secret export does not identify the tracked archive primary key")
    primary_fields = primary_records[0][2]
    if len(primary_fields) <= 14 or primary_fields[14] != "#":
        raise ValueError("archive primary secret must be unavailable in the signing keyring")
    if (
        primary_fields[1] in {"d", "e", "i", "r"}
        or "c" not in primary_fields[11].lower()
        or not primary_fields[5].isdecimal()
        or not primary_fields[6].isdecimal()
    ):
        raise ValueError("archive primary certificate is unusable or has no finite expiry")
    available = [record for record in subkey_records if len(record[2]) > 14 and record[2][14] == "+"]
    if len(available) != 1 or available[0][1] != active_fingerprint:
        raise ValueError("signing keyring must expose exactly the tracked active secret subkey")
    fields = available[0][2]
    if fields[1] in {"d", "e", "i", "r"} or "s" not in fields[11].lower():
        raise ValueError("active archive secret subkey is unusable or cannot sign")
    if not fields[5].isdecimal() or not fields[6].isdecimal():
        raise ValueError("active archive secret subkey must have a finite expiry")
    required = valid_until_epoch + clock_skew_seconds + safety_seconds
    if int(primary_fields[6]) < required:
        raise ValueError("archive primary certificate expires inside the configured safety horizon")
    if int(fields[6]) < required:
        raise ValueError("active signing subkey expires inside the configured safety horizon")
    return {
        "primary_fingerprint": primary_fingerprint,
        "primary_secret": "UNAVAILABLE",
        "primary_expires_epoch": int(primary_fields[6]),
        "active_signing_subkey_fingerprint": active_fingerprint,
        "available_secret_subkeys": 1,
        "created_epoch": int(fields[5]),
        "expires_epoch": int(fields[6]),
        "required_valid_through_epoch": required,
    }


def sign(
    source: pathlib.Path,
    target: pathlib.Path,
    *,
    gpg_home: pathlib.Path,
    fingerprint: str,
    passphrase_file: pathlib.Path,
    clearsign: bool = False,
) -> None:
    mode = "--clearsign" if clearsign else "--detach-sign"
    armor = [] if clearsign else ["--armor"]
    with passphrase_file.open("rb") as passphrase:
        result = subprocess.run(
            [
                "gpg",
                "--homedir",
                str(gpg_home),
                "--batch",
                "--yes",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--local-user",
                f"{fingerprint}!",
                "--digest-algo",
                "SHA512",
                *armor,
                mode,
                "--output",
                str(target),
                str(source),
            ],
            stdin=passphrase,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
        )
    if result.returncode:
        raise RuntimeError(f"OpenPGP signing failed: {result.stderr.decode(errors='replace')[-1000:]}")


def media_type(path: pathlib.Path) -> str:
    name = path.name
    if name.endswith(".deb"):
        return "application/vnd.debian.binary-package"
    if name == "dkc-archive-keyring.gpg":
        return "application/pgp-keys"
    if name.endswith((".dsc", ".changes", ".buildinfo", ".asc", ".gpg")):
        return "application/pgp-signature" if name.endswith((".asc", ".gpg")) else "text/plain"
    if name.endswith(".json"):
        return "application/json"
    if name.endswith(".xz"):
        return "application/x-xz"
    if name.endswith(".gz"):
        return "application/gzip"
    guessed = mimetypes.guess_type(name)[0]
    return guessed or "application/octet-stream"


def artifact_for(root: pathlib.Path, path: pathlib.Path) -> object:
    from dkc.records import Artifact

    key = path.relative_to(root).as_posix()
    immutable = key.startswith("pool/") or "/by-hash/SHA256/" in key
    return Artifact(
        key=key,
        sha256=sha256(path),
        size=path.stat().st_size,
        media_type=media_type(path),
        cache_class="immutable" if immutable else "mutable",
    )


def write_release(
    root: pathlib.Path, release_date: str, valid_until: str
) -> pathlib.Path:
    target = root / "dists/trixie/Release"
    pending = root / ".Release.pending"
    command = [
        "apt-ftparchive",
        "-o",
        "APT::FTPArchive::Release::Origin=DKC",
        "-o",
        "APT::FTPArchive::Release::Label=DKC",
        "-o",
        "APT::FTPArchive::Release::Suite=trixie",
        "-o",
        "APT::FTPArchive::Release::Codename=trixie",
        "-o",
        "APT::FTPArchive::Release::Architectures=amd64",
        "-o",
        "APT::FTPArchive::Release::Components=main",
        "-o",
        "APT::FTPArchive::Release::Acquire-By-Hash=yes",
        "-o",
        f"APT::FTPArchive::Release::Date={release_date}",
        "-o",
        f"APT::FTPArchive::Release::Valid-Until={valid_until}",
        "-o",
        "APT::FTPArchive::Release::MD5=false",
        "-o",
        "APT::FTPArchive::Release::SHA1=false",
        "-o",
        "APT::FTPArchive::Release::SHA512=false",
        "release",
        "dists/trixie",
    ]
    run_to_file(command, pending, root)
    release_text = pending.read_text(encoding="utf-8")
    if "Valid-Until:" not in release_text:
        date_line = f"Date: {release_date}\n"
        if release_text.count(date_line) != 1:
            raise ValueError("apt-ftparchive omitted both Valid-Until and the exact Date")
        release_text = release_text.replace(
            date_line, date_line + f"Valid-Until: {valid_until}\n", 1
        )
    if any(line.split()[-1:] == ["Release"] for line in release_text.splitlines()):
        raise ValueError("Release metadata contains a self-referential checksum")
    if any(section in release_text for section in ("MD5Sum:\n", "SHA1:\n", "SHA512:\n")):
        raise ValueError("Release must advertise SHA256 as its only index digest")
    target.write_text(release_text, encoding="utf-8")
    pending.unlink()
    fields = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        if line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        fields[key] = value.strip()
    expected = {
        "Acquire-By-Hash": "yes",
        "Architectures": "amd64",
        "Codename": "trixie",
        "Components": "main",
        "Date": release_date,
        "Label": "DKC",
        "Origin": "DKC",
        "Suite": "trixie",
        "Valid-Until": valid_until,
    }
    if any(fields.get(key) != value for key, value in expected.items()):
        raise ValueError(f"generated Release identity differs: {fields}")
    expected_checksums = {
        pathlib.PurePosixPath(relative).relative_to("dists/trixie").as_posix(): (
            sha256(root / relative),
            (root / relative).stat().st_size,
        )
        for relative in INDEX_PATHS
    }
    actual_checksums: dict[str, tuple[str, int]] = {}
    in_sha256 = False
    for line in target.read_text(encoding="utf-8").splitlines():
        if line == "SHA256:":
            in_sha256 = True
            continue
        if in_sha256 and line[:1].isspace():
            parts = line.split()
            if len(parts) != 3 or not parts[1].isdecimal():
                raise ValueError("Release contains a malformed SHA256 entry")
            digest, size, relative = parts
            if relative in actual_checksums:
                raise ValueError(f"Release contains duplicate SHA256 entry: {relative}")
            actual_checksums[relative] = (digest, int(size))
            continue
        if in_sha256:
            break
    if actual_checksums != expected_checksums:
        raise ValueError(
            "Release SHA256 graph differs from the six generated indexes: "
            f"{actual_checksums}"
        )
    return target


def repository_artifact_records(root: pathlib.Path) -> list[dict[str, object]]:
    from dkc.records import Artifact

    records: list[dict[str, object]] = []
    for prefix in ("pool", "dists", "keys"):
        prefix_root = root / prefix
        for path in sorted(prefix_root.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"repository contains a symlink: {path}")
            if path.is_file():
                artifact = artifact_for(root, path)
                if not isinstance(artifact, Artifact):
                    raise AssertionError("artifact constructor returned an unexpected type")
                records.append(artifact.to_dict())
            elif not path.is_dir():
                raise ValueError(f"repository contains a special entry: {path}")
    return records


def validate_unsigned_handoff(
    root: pathlib.Path,
    request: dict[str, object],
    *,
    public_keyring: pathlib.Path,
    primary_fingerprint: pathlib.Path,
    signing_subkeys: pathlib.Path,
) -> list[dict[str, object]]:
    from dkc.records import Artifact
    from dkc.schema import validate

    validate("repository-signing-request", request)
    expected_meta_packages = {
        f"dkc-linux-{role}-{flavor}-amd64": request["dkc_version"]
        for role in ("base", "image", "headers")
        for flavor in RELEASE_FLAVORS
    }
    if request["meta_packages"] != expected_meta_packages:
        raise ValueError("signing request does not bind the exact release meta-package set")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"unsigned repository contains a symlink: {path}")
        if path.is_file():
            relative = path.relative_to(root)
            if not relative.parts or relative.parts[0] not in {"pool", "dists", "keys"}:
                raise ValueError(f"unsigned repository contains an unrequested path: {relative}")
        elif not path.is_dir():
            raise ValueError(f"unsigned repository contains a special entry: {path}")
    issued = datetime.fromtimestamp(int(request["issued_epoch"]), timezone.utc)
    expected_release_date = format_datetime(issued, usegmt=True)
    expected_valid_until = format_datetime(
        issued + timedelta(seconds=VALIDITY_SECONDS), usegmt=True
    )
    if (
        request["release_date"] != expected_release_date
        or request["valid_until"] != expected_valid_until
    ):
        raise ValueError("signing request dates do not match its issuance epoch")
    signing_fingerprints = request["signing_subkey_fingerprints"]
    if (
        not isinstance(signing_fingerprints, list)
        or request["active_signing_subkey_fingerprint"] != signing_fingerprints[-1]
    ):
        raise ValueError("signing request does not select its final signing subkey")
    release_fields: dict[str, list[str]] = {}
    for line in (root / "dists/trixie/Release").read_text(encoding="utf-8").splitlines():
        if line[:1].isspace() or ":" not in line:
            continue
        name, value = line.split(":", 1)
        release_fields.setdefault(name, []).append(value.strip())
    if release_fields.get("Date") != [expected_release_date] or release_fields.get(
        "Valid-Until"
    ) != [expected_valid_until]:
        raise ValueError("Release dates differ from the strict signing request")
    raw_records = request["artifacts"]
    if not isinstance(raw_records, list):
        raise ValueError("signing request artifact inventory is malformed")
    expected: dict[str, dict[str, object]] = {}
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise ValueError("signing request contains a malformed artifact")
        artifact = Artifact(**raw)
        if artifact.key in expected:
            raise ValueError(f"signing request repeats an artifact: {artifact.key}")
        expected[artifact.key] = raw
    actual = repository_artifact_records(root)
    actual_by_key = {str(record["key"]): record for record in actual}
    if actual_by_key != expected:
        raise ValueError("unsigned repository bytes differ from the strict signing request")
    tracked = {
        "keys/dkc-archive-keyring.gpg": public_keyring,
        "keys/archive-primary.fingerprint": primary_fingerprint,
        "keys/archive-signing-subkeys.fingerprints": signing_subkeys,
    }
    for key, source in tracked.items():
        target = root / key
        if not source.is_file() or source.is_symlink() or target.read_bytes() != source.read_bytes():
            raise ValueError(f"unsigned repository does not contain the tracked {key}")
    return raw_records


def assemble(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flat", type=pathlib.Path)
    parser.add_argument("--identity", type=pathlib.Path)
    parser.add_argument("--maintenance", action="store_true")
    parser.add_argument("--keyring-bundle", type=pathlib.Path, required=True)
    parser.add_argument("--public-keyring", type=pathlib.Path, required=True)
    parser.add_argument("--primary-fingerprint", type=pathlib.Path, required=True)
    parser.add_argument("--signing-subkeys", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--request", type=pathlib.Path, required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--previous-pool-result", type=pathlib.Path)
    parser.add_argument("--previous-state-result", type=pathlib.Path)
    parser.add_argument("--retention-mode", choices=("series", "series-size"), required=True)
    parser.add_argument("--retention-max-bytes", type=int)
    args = parser.parse_args(arguments)

    root = args.output.resolve()
    build_mode_valid = args.flat is not None and args.identity is not None
    maintenance_mode_valid = (
        args.maintenance
        and args.flat is None
        and args.identity is None
        and args.previous_pool_result is not None
        and args.previous_state_result is not None
    )
    if (
        root.exists()
        or args.request.exists()
        or (args.previous_pool_result is None) != (args.previous_state_result is None)
        or (args.retention_mode == "series" and args.retention_max_bytes is not None)
        or (
            args.retention_mode == "series-size"
            and (args.retention_max_bytes is None or args.retention_max_bytes < 1)
        )
        or (args.maintenance and not maintenance_mode_valid)
        or (not args.maintenance and not build_mode_valid)
    ):
        raise SystemExit("invalid unsigned repository assembly request")
    root.mkdir(parents=True)
    args.request.parent.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from dkc.debver import DebianVersion
    from dkc.records import GcQueueEntry
    from dkc.schema import validate
    from dkc.serialize import dumps
    from dkc.sourcepackage import validate_source_bundle
    from dkc.handoffs import (
        load_authoritative_state_handoff,
        load_live_pool_handoff,
    )

    previous_manifest = None
    previous_state = None
    previous_pool = None
    if args.previous_state_result is not None:
        previous_state = load_authoritative_state_handoff(
            args.previous_state_result,
            keyring=args.public_keyring,
            signing_subkeys=args.signing_subkeys,
        )
        if previous_state is None:
            raise SystemExit("non-bootstrap assembly received empty signed state")
        previous_manifest = previous_state.manifest
        if args.generation != previous_manifest.generation + 1:
            raise SystemExit("repository generation does not follow the previous manifest")
        assert args.previous_pool_result is not None
        previous_pool = load_live_pool_handoff(
            args.previous_pool_result,
            previous_state,
        )
        expected_pool = {
            artifact.key: artifact
            for artifact in previous_manifest.artifacts
            if artifact.key.startswith("pool/") and artifact.key in previous_manifest.live_objects
        }
        actual_pool: set[str] = set()
        for source in sorted(previous_pool.rglob("*")):
            if source.is_symlink() or (not source.is_file() and not source.is_dir()):
                raise SystemExit("previous pool contains an unsafe filesystem entry")
            if source.is_file():
                relative = pathlib.PurePosixPath("pool") / source.relative_to(previous_pool)
                key = relative.as_posix()
                artifact = expected_pool.get(key)
                if (
                    artifact is None
                    or artifact.size != source.stat().st_size
                    or artifact.sha256 != sha256(source)
                    or artifact.cache_class != "immutable"
                ):
                    raise SystemExit("previous pool differs from its signed live inventory")
                actual_pool.add(key)
                copy_exact(source, root / relative)
        if actual_pool != set(expected_pool):
            raise SystemExit("previous pool export is incomplete")
    elif args.generation != 0:
        raise SystemExit("non-bootstrap assembly requires the previous signed state")

    with tempfile.TemporaryDirectory(prefix="public-key-validation-") as directory:
        primary, subkeys, _inventory = validate_public_material(
            args.public_keyring,
            args.primary_fingerprint,
            args.signing_subkeys,
            pathlib.Path(directory),
        )

    identity: dict[str, object] | None = None
    if args.maintenance:
        assert previous_manifest is not None
        package_version = previous_manifest.dkc_version
        debian_source_version = previous_manifest.source_version
        release_meta = sorted(previous_manifest.meta_packages)
        binaries: list[str] = []
    else:
        assert args.identity is not None and args.flat is not None
        identity = json.loads(args.identity.read_text(encoding="utf-8"))
        try:
            source_binaries, binaries, release_meta = release_package_inventory(identity)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        package_version = identity.get("package_version")
        debian_source_version = identity.get("debian_source_version")
        if not isinstance(package_version, str) or not isinstance(debian_source_version, str):
            raise SystemExit("publication identity lacks source versions")
        upstream = DebianVersion.parse(debian_source_version).upstream_release
        with tempfile.TemporaryDirectory(prefix="source-validation-") as directory:
            validation_root = pathlib.Path(directory)
            for source in args.flat.iterdir():
                if (
                    source.is_file()
                    and source.name.startswith("dkc-linux_")
                    and not source.name.endswith(".deb")
                    and source.name.endswith((".dsc", ".tar.xz", ".changes", ".buildinfo"))
                ):
                    copy_exact(source, validation_root / source.name)
            validate_source_bundle(
                validation_root,
                package="dkc-linux",
                version=package_version,
                upstream_version=upstream,
                expected_binary_packages=source_binaries,
            )

    binary_pool = root / "pool/main/d/dkc-linux"
    if not args.maintenance:
        assert args.flat is not None
        found_packages: set[str] = set()
        for source in sorted(args.flat.glob("*.deb")):
            package = subprocess.check_output(
                ["dpkg-deb", "--showformat=${binary:Package}", "--show", source],
                text=True,
                encoding="utf-8",
            )
            if package not in binaries or package in found_packages:
                raise SystemExit(f"unexpected or duplicate binary package: {package}")
            found_packages.add(package)
            copy_or_reuse_exact(source, binary_pool / source.name)
        if found_packages != set(binaries):
            raise SystemExit("flat input does not contain the exact 18-package release graph")
        for source in sorted(args.flat.iterdir()):
            if (
                source.is_file()
                and source.name.startswith("dkc-linux_")
                and source.name.endswith((".dsc", ".orig.tar.xz", ".debian.tar.xz"))
            ):
                copy_or_reuse_exact(source, binary_pool / source.name)

    keyring_pool = root / "pool/main/d/dkc-archive-keyring"
    keyring_files = [
        path
        for path in sorted(args.keyring_bundle.iterdir())
        if path.is_file() and path.name != "bundle.sha256"
    ]
    if len(keyring_files) != 5 or sum(path.suffix == ".deb" for path in keyring_files) != 1:
        raise SystemExit("archive-keyring bundle is not the expected source/binary set")
    for source in keyring_files:
        if source.name.endswith((".deb", ".dsc", ".tar.xz")):
            copy_or_reuse_exact(source, keyring_pool / source.name)

    previous_pool_bytes = 0
    if previous_manifest is not None:
        previous_pool_bytes = sum(
            artifact.size
            for artifact in previous_manifest.artifacts
            if artifact.key.startswith("pool/")
            and artifact.key in previous_manifest.live_objects
        )
        assert previous_state is not None
        if previous_state.storage_size < previous_pool_bytes:
            raise SystemExit("authoritative storage size is smaller than its signed live pool")
        existing_non_pool_bytes = previous_state.storage_size - previous_pool_bytes
    else:
        existing_non_pool_bytes = 0
    keyring_pool_bytes = sum(
        path.stat().st_size for path in keyring_pool.iterdir() if path.is_file()
    )
    # Mutable indexes replace existing objects, while each publication also
    # creates a small immutable signed audit set. Keep explicit headroom so the
    # exact pre-commit whole-bucket check remains comfortably below the cap.
    publication_reserve_bytes = 64 * 1024 * 1024
    retention = apply_retention(
        binary_pool,
        mode=args.retention_mode,
        max_bytes=args.retention_max_bytes,
        fixed_bytes=(
            existing_non_pool_bytes + keyring_pool_bytes + publication_reserve_bytes
        ),
    )
    retained_series = retention.retained_series

    binary_index = root / "dists/trixie/main/binary-amd64/Packages"
    source_index = root / "dists/trixie/main/source/Sources"
    binary_index.parent.mkdir(parents=True)
    source_index.parent.mkdir(parents=True)
    run_to_file(["dpkg-scanpackages", "--multiversion", "pool", "/dev/null"], binary_index, root)
    run_to_file(["dpkg-scansources", "pool", "/dev/null"], source_index, root)
    package_records = parse_control_records(binary_index)
    packages = [record.get("Package", "") for record in package_records]
    if args.maintenance:
        current_names = [
            record.get("Package", "")
            for record in package_records
            if record.get("Version") == package_version
            and record.get("Package") != "dkc-archive-keyring"
        ]
        if (
            len(current_names) != 18
            or len(set(current_names)) != 18
            or not all(name.startswith("dkc-linux-") for name in current_names)
            or not set(release_meta) <= set(current_names)
        ):
            raise SystemExit("maintenance pool lacks the exact current release graph")
        binaries = sorted(current_names)
    if not package_records or not set(packages) <= set(binaries) | {"dkc-archive-keyring"}:
        raise SystemExit("Packages index contains an unexpected binary package")
    for package in binaries:
        current = [
            record
            for record in package_records
            if record.get("Package") == package and record.get("Version") == package_version
        ]
        if len(current) != 1:
            raise SystemExit("Packages index lacks one exact current release package")
    if packages.count("dkc-archive-keyring") < 1 or len(package_records) < 19:
        raise SystemExit("Packages index lacks the archive keyring or current release graph")
    source_records = parse_control_records(source_index)
    source_packages = [record.get("Package", "") for record in source_records]
    if (
        not source_records
        or not set(source_packages) <= {"dkc-archive-keyring", "dkc-linux"}
        or sum(
            record.get("Package") == "dkc-linux" and record.get("Version") == package_version
            for record in source_records
        )
        != 1
        or source_packages.count("dkc-archive-keyring") < 1
        or len(source_records) < 2
    ):
        raise SystemExit("Sources index lacks the exact current source release")
    compress_index(binary_index)
    compress_index(source_index)

    issued = datetime.fromtimestamp(args.epoch, timezone.utc)
    valid_until_dt = issued + timedelta(seconds=VALIDITY_SECONDS)
    release_date = format_datetime(issued, usegmt=True)
    valid_until = format_datetime(valid_until_dt, usegmt=True)
    write_release(root, release_date, valid_until)
    for relative in INDEX_PATHS:
        source = root / relative
        copy_exact(source, source.parent / "by-hash/SHA256" / sha256(source))
    copy_exact(args.public_keyring, root / "keys/dkc-archive-keyring.gpg")
    copy_exact(args.primary_fingerprint, root / "keys/archive-primary.fingerprint")
    copy_exact(
        args.signing_subkeys,
        root / "keys/archive-signing-subkeys.fingerprints",
    )

    if args.maintenance:
        assert previous_manifest is not None
        build_id = previous_manifest.build_id
        dkc_revision = previous_manifest.dkc_revision
        build_policy_sha256 = previous_manifest.build_policy_sha256
        lto_mode = previous_manifest.lto_mode
        source_dsc_sha256 = previous_manifest.source_dsc_sha256
        meta_packages = previous_manifest.meta_packages
    else:
        assert identity is not None
        build_id = identity.get("build_id")
        build_inputs = identity.get("build_inputs")
        if not isinstance(build_id, str) or not isinstance(build_inputs, dict):
            raise SystemExit("publication identity lacks build provenance")
        dkc_revision = build_inputs.get("dkc_revision")
        if not isinstance(dkc_revision, int):
            raise SystemExit("publication identity lacks the downstream revision")
        build_policy_sha256 = build_inputs.get("overlay_sha256")
        if not isinstance(build_policy_sha256, str):
            raise SystemExit("publication identity lacks the build-policy digest")
        lto_mode = build_inputs.get("lto_mode")
        if lto_mode not in ("none", "thin", "full"):
            raise SystemExit("publication identity lacks the LTO policy")
        debian_source = build_inputs.get("debian_source")
        if not isinstance(debian_source, dict):
            raise SystemExit("publication identity lacks the Debian source identity")
        source_dsc_sha256 = debian_source.get("dsc_sha256")
        if not isinstance(source_dsc_sha256, str):
            raise SystemExit("publication identity lacks the Debian source descriptor hash")
        meta_packages = {str(name): package_version for name in release_meta}
    artifacts = repository_artifact_records(root)
    current_keys = {str(record["key"]) for record in artifacts}
    gc_queue = list(previous_manifest.gc_queue) if previous_manifest is not None else []
    tombstoned = {entry.key for entry in gc_queue}
    if current_keys & tombstoned:
        raise SystemExit("a permanently tombstoned object became live again")
    if previous_manifest is not None:
        for artifact in previous_manifest.artifacts:
            if (
                artifact.cache_class == "immutable"
                and artifact.key in previous_manifest.live_objects
                and artifact.key not in current_keys
                and artifact.key not in tombstoned
            ):
                entry = GcQueueEntry(
                    key=artifact.key,
                    sha256=artifact.sha256,
                    size=artifact.size,
                    reason="retired by the signed repository retention policy",
                )
                gc_queue.append(entry)
                tombstoned.add(entry.key)
    request = {
        "schema": "dkc.repository-signing-request.v1",
        "status": "READY",
        "generation": args.generation,
        "issued_epoch": args.epoch,
        "release_date": release_date,
        "valid_until": valid_until,
        "primary_fingerprint": primary,
        "signing_subkey_fingerprints": subkeys,
        "active_signing_subkey_fingerprint": subkeys[-1],
        "source_version": debian_source_version,
        "source_dsc_sha256": source_dsc_sha256,
        "dkc_version": package_version,
        "dkc_revision": dkc_revision,
        "build_policy_sha256": build_policy_sha256,
        "lto_mode": lto_mode,
        "build_id": build_id,
        "retained_series": [list(series) for series in retained_series],
        "retention_mode": retention.mode,
        "meta_packages": meta_packages,
        "package_count": len(packages),
        "source_count": len(source_packages),
        "gc_queue": [entry.to_dict() for entry in gc_queue],
        "artifacts": artifacts,
    }
    if retention.max_bytes is not None:
        request["retention_max_bytes"] = retention.max_bytes
    if previous_manifest is not None:
        request["previous_publication"] = {
            "publication_id": previous_manifest.publication_id,
            "generation": previous_manifest.generation,
        }
    validate("repository-signing-request", request)
    args.request.write_text(dumps(request), encoding="utf-8")
    print(
        json.dumps(
            {
                "active_signing_subkey_fingerprint": subkeys[-1],
                "artifact_count": len(request["artifacts"]),
                "package_count": len(packages),
                "request_sha256": sha256(args.request),
                "source_count": len(source_packages),
                "status": "PASS",
                "valid_until": valid_until,
            },
            sort_keys=True,
        )
    )
    return 0


def sign_handoff(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unsigned", type=pathlib.Path, required=True)
    parser.add_argument("--request", type=pathlib.Path, required=True)
    parser.add_argument("--public-keyring", type=pathlib.Path, required=True)
    parser.add_argument("--primary-fingerprint", type=pathlib.Path, required=True)
    parser.add_argument("--signing-subkeys", type=pathlib.Path, required=True)
    parser.add_argument("--secret-subkey", type=pathlib.Path, required=True)
    parser.add_argument("--passphrase-file", type=pathlib.Path, required=True)
    parser.add_argument("--gpg-home", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--lifecycle-decision", type=pathlib.Path)
    parser.add_argument("--clock-skew-seconds", type=int, required=True)
    parser.add_argument("--safety-seconds", type=int, required=True)
    args = parser.parse_args(arguments)

    if args.output.exists() or any(value < 0 for value in (args.clock_skew_seconds, args.safety_seconds)):
        raise SystemExit("invalid or pre-existing signing output")
    if not args.secret_subkey.is_file() or args.secret_subkey.is_symlink():
        raise SystemExit("secret signing subkey is not a plain file")
    if not args.passphrase_file.is_file() or args.passphrase_file.is_symlink():
        raise SystemExit("signing passphrase is not a plain file")
    args.output.mkdir(parents=True)
    args.gpg_home.mkdir(mode=0o700)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    from dkc.records import (
        Artifact,
        GcQueueEntry,
        LtoMode,
        PublicationManifest,
        StatePointer,
        TransactionRecord,
    )
    from dkc.schema import validate
    from dkc.serialize import dumps

    request_value = json.loads(args.request.read_text(encoding="utf-8"))
    if not isinstance(request_value, dict):
        raise SystemExit("repository signing request is not an object")
    records = validate_unsigned_handoff(
        args.unsigned,
        request_value,
        public_keyring=args.public_keyring,
        primary_fingerprint=args.primary_fingerprint,
        signing_subkeys=args.signing_subkeys,
    )
    if args.lifecycle_decision is not None:
        from dkc.release_gate import require_signing_request_matches_decision

        require_signing_request_matches_decision(
            args.lifecycle_decision,
            args.request,
        )
    primary, subkeys, _inventory = validate_public_material(
        args.public_keyring,
        args.primary_fingerprint,
        args.signing_subkeys,
        args.gpg_home,
    )
    active = str(request_value["active_signing_subkey_fingerprint"])
    if (
        primary != request_value["primary_fingerprint"]
        or subkeys != request_value["signing_subkey_fingerprints"]
        or active != subkeys[-1]
    ):
        raise SystemExit("signing request key identity differs from the tracked public material")
    public_import = subprocess.run(
        [
            "gpg",
            "--homedir",
            str(args.gpg_home),
            "--batch",
            "--import",
            str(args.public_keyring),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if public_import.returncode:
        raise SystemExit("tracked public archive key import failed")
    imported = subprocess.run(
        ["gpg", "--homedir", str(args.gpg_home), "--batch", "--import", str(args.secret_subkey)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if imported.returncode:
        raise SystemExit("secret signing subkey import failed")
    valid_until_epoch = int(request_value["issued_epoch"]) + VALIDITY_SECONDS
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    if int(request_value["issued_epoch"]) > now_epoch + args.clock_skew_seconds:
        raise SystemExit("repository signing request is dated too far in the future")
    if valid_until_epoch < now_epoch + args.clock_skew_seconds:
        raise SystemExit("repository signing request is too old to sign")
    key_report = check_secret_subkey(
        args.gpg_home,
        primary_fingerprint=primary,
        active_fingerprint=active,
        valid_until_epoch=valid_until_epoch,
        clock_skew_seconds=args.clock_skew_seconds,
        safety_seconds=args.safety_seconds,
    )
    with tempfile.TemporaryDirectory(prefix="secret-public-validation-") as directory:
        exported = pathlib.Path(directory) / "public.gpg"
        with exported.open("wb") as stream:
            subprocess.run(
                ["gpg", "--homedir", str(args.gpg_home), "--batch", "--export", primary],
                stdout=stream,
                check=True,
            )
        if exported.read_bytes() != args.public_keyring.read_bytes():
            raise SystemExit("secret subkey export carries different public archive material")

    release = args.unsigned / "dists/trixie/Release"
    inrelease = args.output / "dists/trixie/InRelease"
    release_signature = args.output / "dists/trixie/Release.gpg"
    inrelease.parent.mkdir(parents=True)
    sign(
        release,
        inrelease,
        gpg_home=args.gpg_home,
        fingerprint=active,
        passphrase_file=args.passphrase_file,
        clearsign=True,
    )
    sign(
        release,
        release_signature,
        gpg_home=args.gpg_home,
        fingerprint=active,
        passphrase_file=args.passphrase_file,
    )
    subprocess.run(
        ["gpgv", "--keyring", str(args.public_keyring), str(inrelease)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["gpgv", "--keyring", str(args.public_keyring), str(release_signature), str(release)],
        check=True,
        stdout=subprocess.DEVNULL,
    )

    artifacts = [Artifact(**record) for record in records]
    artifacts.extend(
        [artifact_for(args.output, inrelease), artifact_for(args.output, release_signature)]
    )
    generation = int(request_value["generation"])
    issued = datetime.fromtimestamp(int(request_value["issued_epoch"]), timezone.utc)
    stamp = issued.strftime("%Y%m%d")
    build_id = str(request_value["build_id"])
    publication_id, transaction_id = signed_publication_ids(
        stamp=stamp,
        generation=generation,
        request=args.request.read_bytes(),
        inrelease=inrelease.read_bytes(),
        release_signature=release_signature.read_bytes(),
    )
    index_hashes = {relative: sha256(args.unsigned / relative) for relative in INDEX_PATHS}
    manifest = PublicationManifest(
        generation=generation,
        publication_id=publication_id,
        transaction_id=transaction_id,
        source_version=str(request_value["source_version"]),
        source_dsc_sha256=str(request_value["source_dsc_sha256"]),
        dkc_version=str(request_value["dkc_version"]),
        dkc_revision=int(request_value["dkc_revision"]),
        build_policy_sha256=str(request_value["build_policy_sha256"]),
        lto_mode=cast(LtoMode, request_value["lto_mode"]),
        build_id=build_id,
        retained_series=request_value["retained_series"],
        artifacts=artifacts,
        live_objects=[item.key for item in artifacts],
        apt_metadata={
            "inrelease_sha256": sha256(inrelease),
            "date": str(request_value["release_date"]),
            "valid_until": str(request_value["valid_until"]),
            "index_hashes": index_hashes,
        },
        meta_packages=request_value["meta_packages"],
        created_utc=issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        retention_mode=request_value["retention_mode"],
        retention_max_bytes=request_value.get("retention_max_bytes"),
        gc_queue=[GcQueueEntry(**item) for item in request_value["gc_queue"]],
        previous_publication=request_value.get("previous_publication"),
    )
    validate("publication-manifest", manifest.to_dict())
    manifest_dir = args.output / f"state/publications/{publication_id}"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(dumps(manifest.to_dict()), encoding="utf-8")
    sign(
        manifest_path,
        manifest_dir / "manifest.json.asc",
        gpg_home=args.gpg_home,
        fingerprint=active,
        passphrase_file=args.passphrase_file,
    )

    transaction = TransactionRecord(
        transaction_id=transaction_id,
        publication_id=publication_id,
        expected_generation=generation,
        intended_inrelease_sha256=sha256(inrelease),
        started_utc=issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        phases=[{"phase": number, "state": "pending"} for number in range(5, 13)],
    )
    validate("transaction", transaction.to_dict())
    transaction_dir = args.output / f"state/transactions/{transaction_id}"
    transaction_dir.mkdir(parents=True)
    transaction_path = transaction_dir / "record.json"
    transaction_path.write_text(dumps(transaction.to_dict()), encoding="utf-8")
    sign(
        transaction_path,
        transaction_dir / "record.json.asc",
        gpg_home=args.gpg_home,
        fingerprint=active,
        passphrase_file=args.passphrase_file,
    )

    pointer = StatePointer(
        generation=generation,
        publication_id=publication_id,
        manifest_key=f"state/publications/{publication_id}/manifest.json",
        manifest_sha256=sha256(manifest_path),
        committed_utc=issued.strftime("%Y-%m-%dT%H:%M:%SZ"),
        previous_generation=generation - 1 if generation else None,
    )
    validate("state-pointer", pointer.to_dict())
    pointer_payload = args.output / "state/current.json"
    pointer_payload.parent.mkdir(exist_ok=True)
    pointer_payload.write_text(dumps(pointer.to_dict()), encoding="utf-8")
    sign(
        pointer_payload,
        args.output / "state/current.asc",
        gpg_home=args.gpg_home,
        fingerprint=active,
        passphrase_file=args.passphrase_file,
        clearsign=True,
    )
    pointer_payload.unlink()

    shutil.copyfile(manifest_path, args.output / "manifest.json")
    sign(
        args.output / "manifest.json",
        args.output / "manifest.json.asc",
        gpg_home=args.gpg_home,
        fingerprint=active,
        passphrase_file=args.passphrase_file,
    )
    checksum_records = {
        str(record["key"]): str(record["sha256"])
        for record in records
    }
    for path in args.output.rglob("*"):
        if path.is_file():
            checksum_records[path.relative_to(args.output).as_posix()] = sha256(path)
    sums = "".join(f"{digest}  {key}\n" for key, digest in sorted(checksum_records.items()))
    (args.output / "SHA256SUMS").write_text(sums, encoding="utf-8")
    sign(
        args.output / "SHA256SUMS",
        args.output / "SHA256SUMS.asc",
        gpg_home=args.gpg_home,
        fingerprint=active,
        passphrase_file=args.passphrase_file,
    )
    args.output.chmod(0o755)
    for path in args.output.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"signature overlay contains a symlink: {path}")
        path.chmod(0o755 if path.is_dir() else 0o644)
    print(
        json.dumps(
            {
                **key_report,
                "generation": generation,
                "publication_id": publication_id,
                "request_sha256": sha256(args.request),
                "status": "PASS",
                "transaction_id": transaction_id,
                "valid_until": request_value["valid_until"],
            },
            sort_keys=True,
        )
    )
    return 0


def verified_signature_fingerprint(
    keyring: pathlib.Path, signature: pathlib.Path, signed: pathlib.Path | None = None
) -> str:
    command = [
        "gpgv",
        "--status-fd=1",
        "--keyring",
        str(keyring),
        str(signature),
    ]
    if signed is not None:
        command.append(str(signed))
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if result.returncode:
        raise ValueError(f"signature verification failed: {signature}")
    fingerprints = []
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"\[GNUPG:\] VALIDSIG ([0-9A-F]{40}) .+", line)
        if match is not None:
            fingerprints.append(match.group(1))
    if len(fingerprints) != 1:
        raise ValueError(f"signature did not produce one VALIDSIG record: {signature}")
    return fingerprints[0]


def merge_and_verify(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--unsigned", type=pathlib.Path, required=True)
    parser.add_argument("--request", type=pathlib.Path, required=True)
    parser.add_argument("--overlay", type=pathlib.Path, required=True)
    parser.add_argument("--public-keyring", type=pathlib.Path, required=True)
    parser.add_argument("--primary-fingerprint", type=pathlib.Path, required=True)
    parser.add_argument("--signing-subkeys", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args(arguments)

    if args.output.exists():
        raise SystemExit("refusing to replace a verified repository output")
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
    request_value = json.loads(args.request.read_text(encoding="utf-8"))
    if not isinstance(request_value, dict):
        raise SystemExit("repository signing request is not an object")
    validate_unsigned_handoff(
        args.unsigned,
        request_value,
        public_keyring=args.public_keyring,
        primary_fingerprint=args.primary_fingerprint,
        signing_subkeys=args.signing_subkeys,
    )
    active = str(request_value["active_signing_subkey_fingerprint"])
    with tempfile.TemporaryDirectory(prefix="final-public-key-validation-") as directory:
        primary, signing_subkeys, _inventory = validate_public_material(
            args.public_keyring,
            args.primary_fingerprint,
            args.signing_subkeys,
            pathlib.Path(directory),
        )
    if (
        request_value["primary_fingerprint"] != primary
        or request_value["signing_subkey_fingerprints"] != signing_subkeys
        or active != signing_subkeys[-1]
    ):
        raise ValueError("signing request key identity differs from tracked public material")

    overlay_files: set[str] = set()
    for path in args.overlay.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"signature overlay contains a symlink: {path}")
        if path.is_file():
            overlay_files.add(path.relative_to(args.overlay).as_posix())
        elif not path.is_dir():
            raise ValueError(f"signature overlay contains a special entry: {path}")
    publication_files = sorted(
        path.relative_to(args.overlay).as_posix()
        for path in (args.overlay / "state/publications").glob("*/manifest.json")
    )
    transaction_files = sorted(
        path.relative_to(args.overlay).as_posix()
        for path in (args.overlay / "state/transactions").glob("*/record.json")
    )
    if len(publication_files) != 1 or len(transaction_files) != 1:
        raise ValueError("signature overlay must contain one publication and transaction")
    expected_overlay = {
        "dists/trixie/InRelease",
        "dists/trixie/Release.gpg",
        "state/current.asc",
        "manifest.json",
        "manifest.json.asc",
        "SHA256SUMS",
        "SHA256SUMS.asc",
        publication_files[0],
        f"{publication_files[0]}.asc",
        transaction_files[0],
        f"{transaction_files[0]}.asc",
    }
    if overlay_files != expected_overlay:
        raise ValueError(
            "signature overlay file set differs: "
            f"missing={sorted(expected_overlay - overlay_files)}, "
            f"unexpected={sorted(overlay_files - expected_overlay)}"
        )
    unsigned_keys = {str(record["key"]) for record in request_value["artifacts"]}
    if unsigned_keys & overlay_files:
        raise ValueError("signature overlay attempts to replace unsigned repository bytes")

    args.output.mkdir(parents=True)
    for source_root in (args.unsigned, args.overlay):
        for source in sorted(source_root.rglob("*")):
            if source.is_file():
                copy_exact(source, args.output / source.relative_to(source_root))

    signature_pairs: list[tuple[pathlib.Path, pathlib.Path | None]] = [
        (args.output / "dists/trixie/InRelease", None),
        (args.output / "dists/trixie/Release.gpg", args.output / "dists/trixie/Release"),
        (args.output / "manifest.json.asc", args.output / "manifest.json"),
        (args.output / "state/current.asc", None),
        (args.output / "SHA256SUMS.asc", args.output / "SHA256SUMS"),
        (
            args.output / f"{publication_files[0]}.asc",
            args.output / publication_files[0],
        ),
        (
            args.output / f"{transaction_files[0]}.asc",
            args.output / transaction_files[0],
        ),
    ]
    for signature, signed in signature_pairs:
        if verified_signature_fingerprint(args.public_keyring, signature, signed) != active:
            raise ValueError(f"signature was not made by the requested active subkey: {signature}")

    checksum_records: dict[str, str] = {}
    for line in (args.output / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._+~/-]*)", line)
        if match is None or match.group(2) in checksum_records:
            raise ValueError("SHA256SUMS contains a malformed or duplicate record")
        pure = pathlib.PurePosixPath(match.group(2))
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError("SHA256SUMS contains an unsafe path")
        checksum_records[match.group(2)] = match.group(1)
    expected_checksum_paths = {
        path.relative_to(args.output).as_posix()
        for path in args.output.rglob("*")
        if path.is_file() and path.name not in {"SHA256SUMS", "SHA256SUMS.asc"}
    }
    if set(checksum_records) != expected_checksum_paths:
        raise ValueError("SHA256SUMS file set differs from the merged repository")
    for key, digest in checksum_records.items():
        if sha256(args.output / key) != digest:
            raise ValueError(f"merged repository checksum differs: {key}")
    args.output.chmod(0o755)
    for path in args.output.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
    print(
        json.dumps(
            {
                "active_signing_subkey_fingerprint": active,
                "overlay_file_count": len(overlay_files),
                "repository_file_count": len(expected_checksum_paths) + 2,
                "request_sha256": sha256(args.request),
                "status": "PASS",
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in {"assemble", "sign", "merge"}:
        raise SystemExit("usage: build-signed-repository.py assemble|sign|merge [options]")
    if sys.argv[1] == "assemble":
        return assemble(sys.argv[2:])
    if sys.argv[1] == "sign":
        return sign_handoff(sys.argv[2:])
    return merge_and_verify(sys.argv[2:])


if __name__ == "__main__":
    raise SystemExit(main())
