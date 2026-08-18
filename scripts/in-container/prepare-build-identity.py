#!/usr/bin/env python3
"""Derive and inject one publication identity shared by all three flavors.

This runs after the authenticated Debian source and the reviewed overlay have
been materialized, but before one matrix flavor is selected.  Consequently the
digest covers the complete flavor policy and is identical in v2/v3/v4 jobs.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.parse


FLAVORS = ("v2", "v3", "v4")
LTO_MODES = ("none", "thin", "full")
LTO_FRAGMENT_MARKER = "# DKC link-time optimization policy."
LTO_CONFIG_LINES = {
    "none": (
        "CONFIG_LTO_NONE=y",
        "# CONFIG_LTO_CLANG_FULL is not set",
        "# CONFIG_LTO_CLANG_THIN is not set",
        "CONFIG_DEBUG_INFO_BTF=y",
        "CONFIG_DEBUG_INFO_BTF_MODULES=y",
    ),
    "thin": (
        "# CONFIG_LTO_NONE is not set",
        "# CONFIG_LTO_CLANG_FULL is not set",
        "CONFIG_LTO_CLANG_THIN=y",
        "# CONFIG_DEBUG_INFO_BTF is not set",
        "# CONFIG_DEBUG_INFO_BTF_MODULES is not set",
    ),
    "full": (
        "# CONFIG_LTO_NONE is not set",
        "CONFIG_LTO_CLANG_FULL=y",
        "# CONFIG_LTO_CLANG_THIN is not set",
        "# CONFIG_DEBUG_INFO_BTF is not set",
        "# CONFIG_DEBUG_INFO_BTF_MODULES is not set",
    ),
}


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def source_hashes(inputs: pathlib.Path, inventory: dict[str, object]) -> tuple[str, dict[str, str]]:
    files = inventory.get("files")
    if not isinstance(files, list) or len(files) != 3:
        raise SystemExit("source inventory must contain exactly dsc, orig, and debian members")

    hashes: dict[str, str] = {}
    dsc_sha: str | None = None
    for item in files:
        if not isinstance(item, dict):
            raise SystemExit("source inventory file record is not an object")
        name, url, expected = item.get("name"), item.get("url"), item.get("sha256")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+~-]*", name)
            or not isinstance(url, str)
            or not isinstance(expected, str)
        ):
            raise SystemExit("source inventory file record lacks safe name/url/sha256 strings")
        if pathlib.PurePosixPath(urllib.parse.urlsplit(url).path).name != name:
            raise SystemExit("source inventory member name differs from its URL")
        member = inputs / name
        if not member.is_file() or member.is_symlink():
            raise SystemExit(f"source member {name!r} is not a staged plain file")
        actual = sha256_file(member)
        if actual != expected:
            raise SystemExit(f"staged source member hash changed: {name}")
        if name.endswith(".dsc"):
            if dsc_sha is not None:
                raise SystemExit("source inventory contains multiple .dsc files")
            dsc_sha = actual
        else:
            hashes[name] = actual

    if dsc_sha is None or len(hashes) != 2:
        raise SystemExit("source inventory does not identify one .dsc and two tar members")
    return dsc_sha, hashes


def dependency_lock(path: pathlib.Path) -> dict[str, str]:
    result: dict[str, str] = {}
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        expected = {"package", "version", "architecture", "uri", "size", "sha256"}
        if set(reader.fieldnames or ()) != expected:
            raise SystemExit(f"unexpected toolchain lock columns: {reader.fieldnames!r}")
        for row in reader:
            package = row["package"]
            architecture = row["architecture"]
            base_package, separator, qualifier = package.partition(":")
            if separator and qualifier != architecture:
                raise SystemExit(
                    f"package architecture qualifier differs from lock column: {package}"
                )
            key = f"{base_package}:{architecture}"
            if key in result:
                raise SystemExit(f"duplicate package in toolchain lock: {key}")
            digest = row["sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise SystemExit(f"malformed package digest in toolchain lock: {key}")
            result[key] = f"{row['version']}@{digest}"
    if not result:
        raise SystemExit("toolchain lock is empty")
    return result


def format_changelog_time(epoch: int) -> str:
    instant = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
    weekdays = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return (
        f"{weekdays[instant.weekday()]}, {instant.day:02d} "
        f"{months[instant.month - 1]} {instant.year:04d} "
        f"{instant:%H:%M:%S} +0000"
    )


def next_utc_date_epoch(epoch: int) -> int:
    """Return midnight starting the first UTC date after the source entry."""

    if epoch < 0:
        raise ValueError("changelog epoch cannot be negative")
    return (epoch // 86400 + 1) * 86400


def parse_kernel_config(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line):
            key, value = match.groups()
        elif match := re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line):
            key, value = match.group(1), "n"
        else:
            continue
        if key in values:
            raise SystemExit(f"duplicate Kconfig symbol in {path}: {key}")
        values[key] = value
    if not values:
        raise SystemExit(f"resolved Kconfig is empty: {path}")
    return values


def apply_lto_policy(source: pathlib.Path, mode: str) -> None:
    """Make the selected LTO choice a reconstructible source-package input."""

    if mode not in LTO_MODES:
        raise SystemExit("kernel LTO mode must be none, thin, or full")
    block = "\n".join((LTO_FRAGMENT_MARKER, *LTO_CONFIG_LINES[mode])) + "\n"
    for flavor in FLAVORS:
        path = source / f"debian/config/amd64/config.{flavor}-amd64"
        if not path.is_file():
            raise SystemExit(f"overlay did not create {path}")
        original = path.read_text(encoding="utf-8")
        if LTO_FRAGMENT_MARKER in original or any(
            line.startswith("CONFIG_LTO_")
            or line.startswith("# CONFIG_LTO_")
            or line.startswith("CONFIG_DEBUG_INFO_BTF")
            or line.startswith("# CONFIG_DEBUG_INFO_BTF")
            for line in original.splitlines()
        ):
            raise SystemExit(
                f"flavor fragment already carries an LTO/BTF policy: {path}"
            )
        path.write_text(original.rstrip() + "\n\n" + block, encoding="utf-8")


def resolve_policy_configs(
    source: pathlib.Path, llvm_major: int
) -> dict[str, dict[str, str]]:
    """Resolve all flavor policies before any BUILD_ID-derived value exists."""

    resolved: dict[str, dict[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix="dkc-policy-config-", dir=source.parent) as name:
        root = pathlib.Path(name)
        for flavor in FLAVORS:
            output = root / flavor
            output.mkdir()
            subprocess.run(
                [
                    str(source / "debian/bin/kconfig.py"),
                    str(output / ".config"),
                    "debian/config/config",
                    "debian/config/amd64/config",
                    f"debian/config/amd64/config.{flavor}-amd64",
                    "-o",
                    'BUILD_SALT=""',
                    "-o",
                    "MODULE_SIG=n",
                ],
                cwd=source,
                check=True,
            )
            subprocess.run(
                [
                    "make",
                    f"LLVM=-{llvm_major}",
                    "ARCH=x86",
                    f"O={output}",
                    "olddefconfig",
                ],
                cwd=source,
                stdout=subprocess.DEVNULL,
                check=True,
            )
            resolved[flavor] = parse_kernel_config(output / ".config")
    return resolved


def inject_identity(source: pathlib.Path, abi: str, source_version: str, package_version: str, epoch: int) -> None:
    defines_path = source / "debian/config/defines.toml"
    defines = defines_path.read_text(encoding="utf-8")
    if "\nabi_name = " in defines:
        raise SystemExit("Debian config already contains an injected ABI identity")
    llvm_lines = re.findall(r"^llvm_major = [0-9]+$", defines, flags=re.MULTILINE)
    if len(llvm_lines) != 1:
        raise SystemExit(f"expected exactly one overlay llvm_major, found {len(llvm_lines)}")
    defines = defines.replace(llvm_lines[0], f"{llvm_lines[0]}\nabi_name = '{abi}'", 1)
    defines_path.write_text(defines, encoding="utf-8")

    changelog_path = source / "debian/changelog"
    changelog = changelog_path.read_text(encoding="utf-8")
    expected_first = f"linux ({source_version}) "
    if not changelog.startswith(expected_first):
        raise SystemExit(
            "Debian changelog does not start with the authenticated source version: "
            f"expected {expected_first!r}"
        )
    entry = (
        f"dkc-linux ({package_version}) trixie; urgency=medium\n\n"
        "  * Build the reviewed v2, v3, and v4 kernel packages.\n\n"
        f" -- DKC Build Service <build@dkc.invalid>  {format_changelog_time(epoch)}\n\n"
    )
    changelog_path.write_text(entry + changelog, encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 6:
        print(
            "usage: prepare-build-identity.py <source-root> <repo-root> "
            "<inputs-root> <dkc-revision> <none|thin|full>",
            file=sys.stderr,
        )
        return 2

    source, repo, inputs = (pathlib.Path(item).resolve() for item in sys.argv[1:4])
    try:
        revision = int(sys.argv[4])
    except ValueError as exc:
        raise SystemExit("DKC revision must be an integer") from exc
    if revision < 1:
        raise SystemExit("DKC revision must be positive")
    lto_mode = sys.argv[5]
    if lto_mode not in LTO_MODES:
        raise SystemExit("kernel LTO mode must be none, thin, or full")

    sys.path.insert(0, str(repo))
    from dkc.buildid import (  # noqa: PLC0415
        BuildInputs,
        normalized_policy_config,
        policy_config_digest,
    )
    from dkc.buildpolicy import (  # noqa: PLC0415
        BUILD_POLICY_REVISION,
        build_policy_digest,
    )
    from dkc.flavors import load_all_flavor_policies  # noqa: PLC0415
    from dkc.naming import Identity, package_names  # noqa: PLC0415
    from dkc.serialize import dumps  # noqa: PLC0415

    inventory = json.loads((inputs / "source-inventory.json").read_text(encoding="utf-8"))
    if inventory.get("schema_version") != 2 or inventory.get("source") != "linux":
        raise SystemExit("unsupported or non-linux source inventory")
    source_version = inventory.get("version")
    source_epoch = inventory.get("source_date_epoch")
    llvm_major = inventory.get("llvm_major")
    if (
        not isinstance(source_version, str)
        or not isinstance(source_epoch, int)
        or not isinstance(llvm_major, int)
        or llvm_major < 1
    ):
        raise SystemExit("source inventory lacks a valid version, epoch, or LLVM major")
    dsc_sha, member_hashes = source_hashes(inputs, inventory)

    overlay_sha = build_policy_digest(repo)
    apply_lto_policy(source, lto_mode)

    policies = load_all_flavor_policies(repo / "config/flavors")
    resolved_configs = resolve_policy_configs(source, llvm_major)
    flavor_hashes: dict[str, str] = {}
    flavor_policy: dict[str, str] = {}
    for flavor in FLAVORS:
        fragment_path = source / f"debian/config/amd64/config.{flavor}-amd64"
        if not fragment_path.is_file():
            raise SystemExit(f"overlay did not create {fragment_path}")
        normalized_path = inputs / f"policy-config-{flavor}.json"
        normalized_path.write_text(
            dumps(normalized_policy_config(resolved_configs[flavor])),
            encoding="utf-8",
        )
        flavor_hashes[flavor] = policy_config_digest(resolved_configs[flavor])
        if sha256_file(normalized_path) != flavor_hashes[flavor]:
            raise SystemExit(f"serialized {flavor} policy configuration hash differs")
        flavor_policy[flavor] = policies[flavor].compiler_march

    lock_path = inputs / "build-image-debs.tsv"
    build_inputs = BuildInputs(
        schema_version=1,
        debian_source_version=source_version,
        dsc_sha256=dsc_sha,
        source_member_sha256=member_hashes,
        dkc_revision=revision,
        overlay_sha256=overlay_sha,
        flavor_config_sha256=flavor_hashes,
        flavor_policy=flavor_policy,
        # A local Podman image ID includes layer creation metadata and may differ
        # across fresh GitHub jobs even when every rootfs byte that can affect
        # the build is identical.  The identity therefore uses the immutable
        # FROM digest here; Containerfile.build is in overlay_sha256 and every
        # installed .deb byte is in the complete dependency lock below.  The
        # actual local image ID remains separately recorded as provenance.
        base_image_digest=(repo / "config/base-image.lock").read_text(encoding="utf-8").strip(),
        toolchain_lock_sha256=sha256_file(lock_path),
        build_policy_revision=BUILD_POLICY_REVISION,
        lto_mode=lto_mode,
        dependency_lock=dependency_lock(lock_path),
    )
    full_digest = build_inputs.digest()
    identity = Identity.create(source_version, revision, build_inputs.build_id())

    publication_epoch = next_utc_date_epoch(source_epoch)
    record = {
        "schema_version": 1,
        "build_input_digest": full_digest,
        "build_id": identity.build_id,
        "source_package": "dkc-linux",
        "debian_source_version": source_version,
        "package_version": identity.package_version,
        "lto_mode": lto_mode,
        "publication_source_date_epoch": publication_epoch,
        "abi": identity.abi,
        "kernel_releases": {
            flavor: identity.kernel_release(flavor) for flavor in FLAVORS
        },
        "package_names": package_names(identity),
        "build_inputs": build_inputs.inventory(),
    }
    inject_identity(
        source,
        identity.abi,
        source_version,
        identity.package_version,
        publication_epoch,
    )
    (inputs / "publication-identity.json").write_text(dumps(record), encoding="utf-8")
    print(
        f"publication identity PASS: build_id={identity.build_id} "
        f"abi={identity.abi} version={identity.package_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
