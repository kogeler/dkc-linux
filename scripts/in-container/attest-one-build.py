#!/usr/bin/env python3
"""Attest one flavor build without walking every module's DWARF."""

from __future__ import annotations

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
import tempfile
from collections import Counter
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from functools import lru_cache


ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dkc.buildid import policy_config_digest  # noqa: E402


LLVM_TOOL = re.compile(
    r"(clang(?:\+\+)?|ld\.lld|llvm-(?:ar|nm|objcopy|objdump|readelf|strip|link))-(\d+)"
)
UNVERSIONED_LLVM_TOOL = re.compile(
    r"(?:clang(?:\+\+)?|ld\.lld|llvm-(?:ar|nm|objcopy|objdump|readelf|strip|link))"
)
GNU_TOOL = re.compile(
    r"(?:(?:[A-Za-z0-9_]+-)?linux-gnu-)?"
    r"(?:gcc|g\+\+|cc|c\+\+|ld|ld\.bfd|ld\.gold|as|ar|nm|objcopy|objdump|readelf|strip)"
    r"(?:-\d+)?"
)
SAVED_COMMAND = re.compile(r"^(?:savedcmd|cmd)_(.+?) := (.*)$")


def fail(message: str) -> None:
    raise SystemExit(f"attestation FAIL: {message}")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def timestamp_epoch(value: str, label: str) -> int:
    try:
        instant = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        fail(f"malformed {label} timestamp: {value!r}")
    if instant.tzinfo is None:
        fail(f"{label} timestamp lacks a timezone: {value!r}")
    return int(instant.timestamp())


def is_possible_private_key(path: pathlib.Path) -> bool:
    """Match private-key containers, not public X.509 certificates or .cmd metadata."""

    return path.suffix.lower() in {".key", ".p12", ".pem", ".pfx"}


def parse_config(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line):
            key, value = match.groups()
        elif match := re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line):
            key, value = match.group(1), "n"
        else:
            continue
        if key in values:
            fail(f"duplicate Kconfig symbol in {path}: {key}")
        values[key] = value
    if not values:
        fail(f"resolved Kconfig is empty: {path}")
    return values


def parse_deb822(path: pathlib.Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line[:1] in (" ", "\t"):
            if current is None:
                fail(f"orphan continuation in {path}")
            fields[current] += "\n" + line[1:]
            continue
        key, separator, value = line.partition(":")
        if not separator or not key or key in fields:
            fail(f"malformed or duplicate Deb822 field in {path}: {line!r}")
        current = key
        fields[key] = value.lstrip()
    return fields


def dependency_names(value: str) -> set[str]:
    return {
        re.split(r"[ (]", item.strip(), maxsplit=1)[0]
        for item in value.replace("\n", " ").split(",")
        if item.strip()
    }


def binary_names(value: str) -> set[str]:
    names = {item for item in re.split(r"[\s,]+", value.strip()) if item}
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", item) for item in names):
        fail(f"malformed binary package inventory: {value!r}")
    return names


def checksum_records(value: str, label: str) -> dict[str, tuple[str, int]]:
    records: dict[str, tuple[str, int]] = {}
    for line in value.splitlines():
        # Deb822 multiline fields commonly have an empty value on their header
        # line, represented by parse_deb822 as one leading newline.
        if not line:
            continue
        fields = line.split()
        if len(fields) != 3:
            fail(f"malformed {label} checksum record: {line!r}")
        digest, size_text, filename = fields
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not size_text.isdigit():
            fail(f"malformed {label} checksum value: {line!r}")
        if pathlib.PurePosixPath(filename).name != filename or filename in records:
            fail(f"unsafe or duplicate {label} checksum filename: {filename!r}")
        records[filename] = (digest, int(size_text))
    if not records:
        fail(f"{label} has no SHA-256 records")
    return records


def require_checksum_set(
    records: dict[str, tuple[str, int]], paths: list[pathlib.Path], label: str
) -> None:
    expected = {path.name for path in paths}
    if set(records) != expected:
        fail(
            f"{label} file set differs: missing={sorted(expected - set(records))}, "
            f"unexpected={sorted(set(records) - expected)}"
        )
    for path in paths:
        if records[path.name] != (sha256(path), path.stat().st_size):
            fail(f"{label} digest/size differs for {path.name}")


def materialize_module(path: pathlib.Path, temporary: pathlib.Path) -> pathlib.Path:
    if path.suffix == ".xz":
        output = temporary / (path.name + ".elf")
        with lzma.open(path, "rb") as source, output.open("wb") as target:
            shutil.copyfileobj(source, target)
        return output
    if path.suffix == ".zst":
        output = temporary / (path.name + ".elf")
        with output.open("wb") as target:
            subprocess.run(["zstd", "-q", "-d", "-c", path], stdout=target, check=True)
        return output
    return path


def readelf(tool: str, path: pathlib.Path, *arguments: str) -> str:
    result = subprocess.run(
        [tool, *arguments, path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        fail(f"{tool} could not inspect {path}: {result.stderr.strip()}")
    return result.stdout


def inspect_elf(tool: str, path: pathlib.Path, label: str, llvm_major: int, require_lld: bool) -> None:
    comment = readelf(tool, path, "--string-dump=.comment")
    if f"clang version {llvm_major}." not in comment:
        fail(f"{label} .comment lacks clang major {llvm_major}")
    if require_lld and f"LLD {llvm_major}." not in comment:
        fail(f"{label} .comment lacks LLD major {llvm_major}")


def has_btf(tool: str, path: pathlib.Path) -> bool:
    sections = readelf(tool, path, "--sections")
    return re.search(r"\]\s+\.BTF\s", sections) is not None


def require_btf(tool: str, path: pathlib.Path, label: str) -> None:
    if not has_btf(tool, path):
        fail(f"{label} has no .BTF section")


def forbid_btf(tool: str, path: pathlib.Path, label: str) -> None:
    if has_btf(tool, path):
        fail(f"{label} unexpectedly has a .BTF section")


def module_identity(module: pathlib.Path, extracted: pathlib.Path) -> str:
    parts = module.relative_to(extracted).parts
    try:
        kernel_index = parts.index("kernel")
    except ValueError:
        fail(f"shipped module has no kernel-relative path: {module}")
    identity = "/".join(parts[kernel_index + 1 :])
    return re.sub(r"\.(?:xz|zst)$", "", identity)


def select_module_sample(modules: dict[str, pathlib.Path], limit: int = 32) -> list[str]:
    """Select deterministic broad and size-biased artifact smoke coverage."""

    selected: list[str] = []

    def add(identity: str) -> None:
        if identity not in selected and len(selected) < limit:
            selected.append(identity)

    for identity in sorted(modules, key=lambda item: (-modules[item].stat().st_size, item))[:8]:
        add(identity)

    by_subsystem: dict[str, list[str]] = {}
    for identity in modules:
        by_subsystem.setdefault(identity.partition("/")[0], []).append(identity)
    for subsystem in sorted(by_subsystem):
        add(sorted(by_subsystem[subsystem])[0])

    for identity in sorted(
        modules,
        key=lambda item: (hashlib.sha256(item.encode("utf-8")).hexdigest(), item),
    ):
        add(identity)
    return selected


def shell_tokens(command: str, label: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
    lexer.commenters = ""
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError as error:
        fail(f"cannot tokenize Kbuild command {label}: {error}")


@lru_cache(maxsize=None)
def resolve_llvm_tool(token: str, llvm_major: int) -> str | None:
    basename = pathlib.PurePath(token).name
    match = LLVM_TOOL.fullmatch(basename)
    if match:
        if int(match.group(2)) != llvm_major:
            fail(f"unexpected LLVM tool major in Kbuild command: {basename}")
        expected = pathlib.Path("/usr/bin") / basename
        resolved_token = shutil.which(token) if "/" not in token else token
        if resolved_token is None:
            fail(f"Kbuild tool does not resolve: {token}")
        if pathlib.Path(resolved_token).resolve() != expected.resolve():
            fail(f"Kbuild tool {token} does not resolve through {expected}")
        return basename
    if UNVERSIONED_LLVM_TOOL.fullmatch(basename):
        fail(f"unexpected unversioned LLVM tool in Kbuild command: {basename}")
    if GNU_TOOL.fullmatch(basename):
        fail(f"unexpected GNU or unversioned tool in Kbuild command: {basename}")
    return None


def iter_saved_commands(cmd_files: list[pathlib.Path]) -> Iterator[tuple[pathlib.Path, str, str]]:
    for path in cmd_files:
        for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
            if match := SAVED_COMMAND.fullmatch(line):
                yield path, match.group(1), match.group(2)


def normalize_target(target: str, build: pathlib.Path) -> str:
    build_text = str(build)
    if target.startswith(build_text + "/"):
        target = target[len(build_text) + 1 :]
    target = target.removeprefix("./")
    normalized = posixpath.normpath(target)
    if (
        target.startswith("/")
        or normalized in ("", ".", "..")
        or normalized.startswith("../")
    ):
        fail(f"unsafe Kbuild command target: {target}")
    return normalized


def load_kbuild_inventory(
    path: pathlib.Path, llvm_major: int
) -> tuple[
    dict[str, list[tuple[pathlib.PurePosixPath, str, list[str]]]],
    int,
    Counter[str],
    str,
]:
    try:
        with lzma.open(path, "rt", encoding="utf-8") as stream:
            normalized_inventory = stream.read()
    except (OSError, EOFError, lzma.LZMAError) as exc:
        fail(f"cannot read captured Kbuild command inventory: {exc}")
    if not normalized_inventory.endswith("\n"):
        fail("captured Kbuild command inventory lacks its final newline")
    lines = normalized_inventory.splitlines()
    if lines != sorted(lines) or len(lines) != len(set(lines)):
        fail("captured Kbuild command inventory is not sorted and unique")

    records: dict[
        str, list[tuple[pathlib.PurePosixPath, str, list[str]]]
    ] = {}
    tool_counts: Counter[str] = Counter()
    for number, line in enumerate(lines, 1):
        fields = line.split("\t", 2)
        if len(fields) != 3:
            fail(f"malformed captured Kbuild record at line {number}")
        target, command_file_text, command = fields
        normalized = posixpath.normpath(target)
        command_file = pathlib.PurePosixPath(command_file_text)
        if (
            target != normalized
            or normalized in ("", ".", "..")
            or normalized.startswith("../")
            or command_file.is_absolute()
            or command_file.as_posix() in ("", ".", "..")
            or command_file.as_posix().startswith("../")
        ):
            fail(f"unsafe captured Kbuild record at line {number}")
        tools: list[str] = []
        for token in shell_tokens(command, target):
            if tool := resolve_llvm_tool(token, llvm_major):
                tools.append(tool)
                tool_counts[tool] += 1
        records.setdefault(target, []).append((command_file, command, tools))
    if not records:
        fail("captured Kbuild command inventory is empty")
    return records, len(lines), tool_counts, normalized_inventory


def capture_replay(build: pathlib.Path, evidence: pathlib.Path, llvm_major: int) -> None:
    if not build.is_dir():
        fail(f"build directory is absent: {build}")
    replay = evidence / "attestation-replay"
    replay.mkdir(parents=True, exist_ok=True)
    inventory_path = evidence / "kbuild-commands.tsv.xz"
    if inventory_path.exists() or (replay / "build-tree-inventory.json").exists():
        fail("refusing to replace captured attestation inputs")

    cmd_files = sorted(build.rglob(".*.cmd"))
    if not cmd_files:
        fail("no Kbuild .cmd files found")
    inventory_lines: list[str] = []
    for path, raw_target, command in iter_saved_commands(cmd_files):
        target = normalize_target(raw_target, build)
        # Validate tool identity now, while the original commands and toolchain
        # still exist. The same tokens are independently checked on replay.
        for token in shell_tokens(command, target):
            resolve_llvm_tool(token, llvm_major)
        inventory_lines.append(f"{target}\t{path.relative_to(build).as_posix()}\t{command}")
    if not inventory_lines:
        fail("Kbuild .cmd files contain no savedcmd records")
    if len(inventory_lines) != len(set(inventory_lines)):
        fail("Kbuild command capture contains duplicate records")
    normalized_inventory = "\n".join(sorted(inventory_lines)) + "\n"
    with lzma.open(inventory_path, "wt", encoding="utf-8", preset=1) as stream:
        stream.write(normalized_inventory)

    module_targets = sorted(
        path.relative_to(build).as_posix()
        for path in build.rglob("*.ko")
        if path.is_file()
    )
    if len(module_targets) < 1000:
        fail(f"refusing incomplete build-module capture: only {len(module_targets)} modules")
    private_keys = sorted(
        path.relative_to(build).as_posix()
        for path in build.rglob("*")
        if path.is_file() and is_possible_private_key(path)
    )
    public_certificates = sorted(
        path.relative_to(build).as_posix()
        for path in build.rglob("*.x509")
        if path.is_file()
    )
    document = {
        "schema_version": 1,
        "status": "COMPLETE" if not private_keys else "FAIL",
        "llvm_major": llvm_major,
        "kbuild_cmd_files": len(cmd_files),
        "kbuild_cmd_records": len(inventory_lines),
        "kbuild_command_inventory_sha256": text_sha256(normalized_inventory),
        "module_targets": module_targets,
        "possible_private_key_files": private_keys,
        "public_x509_files": public_certificates,
    }
    (replay / "build-tree-inventory.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if private_keys:
        fail(f"build produced possible private signing material: {private_keys[:5]}")
    print(
        f"attestation capture COMPLETE: {len(inventory_lines)} Kbuild records, "
        f"{len(module_targets)} module targets",
        file=sys.stderr,
    )


def require_relative_symlink(
    extracted: pathlib.Path, relative: str, expected_target: str
) -> None:
    path = extracted / relative
    if not path.is_symlink():
        fail(f"required package symlink is absent: {relative}")
    link_target = path.readlink().as_posix()
    if pathlib.PurePosixPath(link_target).is_absolute():
        fail(f"package symlink is absolute: {relative} -> {link_target}")
    resolved = posixpath.normpath(
        posixpath.join(posixpath.dirname(relative), link_target)
    )
    if resolved != expected_target:
        fail(
            f"package symlink resolves incorrectly: {relative} -> "
            f"{resolved}, expected {expected_target}"
        )


def validate_kernelvariables(path: pathlib.Path, llvm_major: int) -> None:
    assignments: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"(?:override )?([A-Z0-9_]+) = ?(.*)", line)
        if not match or match.group(1) not in {
            "LLVM",
            "LLVM_PREFIX",
            "LLVM_SUFFIX",
            "CC",
            "HOSTCC",
            "HOSTCXX",
            "LD",
            "AR",
            "NM",
            "OBJCOPY",
            "OBJDUMP",
            "READELF",
            "STRIP",
            "LLVM_LINK",
        }:
            continue
        key, value = match.groups()
        if key in assignments:
            fail(f"duplicate header tool assignment in {path}: {key}")
        assignments[key] = value
    expected = {
        "LLVM": f"-{llvm_major}",
        "LLVM_PREFIX": "",
        "LLVM_SUFFIX": f"-{llvm_major}",
        "CC": f"$(if $(DEBIAN_KERNEL_USE_CCACHE),$(CCACHE)) clang-{llvm_major}",
        "HOSTCC": f"clang-{llvm_major}",
        "HOSTCXX": f"clang++-{llvm_major}",
        "LD": f"ld.lld-{llvm_major}",
        "AR": f"llvm-ar-{llvm_major}",
        "NM": f"llvm-nm-{llvm_major}",
        "OBJCOPY": f"llvm-objcopy-{llvm_major}",
        "OBJDUMP": f"llvm-objdump-{llvm_major}",
        "READELF": f"llvm-readelf-{llvm_major}",
        "STRIP": f"llvm-strip-{llvm_major}",
        "LLVM_LINK": f"llvm-link-{llvm_major}",
    }
    for key, value in expected.items():
        if assignments.get(key) != value:
            fail(f"header tool assignment {key}={assignments.get(key)!r}, expected {value!r}")


def main() -> int:
    if len(sys.argv) == 5 and sys.argv[1] == "--capture-replay":
        build = pathlib.Path(sys.argv[2])
        evidence = pathlib.Path(sys.argv[3])
        try:
            llvm_major = int(sys.argv[4])
        except ValueError:
            fail("LLVM major is invalid")
        capture_replay(build, evidence, llvm_major)
        return 0

    replay_evidence: pathlib.Path | None = None
    if len(sys.argv) == 10 and sys.argv[8] == "--replay-evidence":
        replay_evidence = pathlib.Path(sys.argv[9])
    elif len(sys.argv) != 8:
        print(
            "usage: attest-one-build.py <source-root> <artifact-dir> <out-dir> "
            "<llvm-major> <publication-identity.json> <v2|v3|v4> <none|thin|full> "
            "[--replay-evidence <evidence-dir>]\n"
            "       attest-one-build.py --capture-replay "
            "<build-dir> <evidence-dir> <llvm-major>",
            file=sys.stderr,
        )
        return 2

    source = pathlib.Path(sys.argv[1])
    artifacts = pathlib.Path(sys.argv[2])
    output = pathlib.Path(sys.argv[3])
    llvm_major = int(sys.argv[4])
    identity = json.loads(pathlib.Path(sys.argv[5]).read_text(encoding="utf-8"))
    flavor = sys.argv[6]
    lto_mode = sys.argv[7]
    if flavor not in ("v2", "v3", "v4"):
        fail(f"unknown flavor {flavor!r}")
    lto_required = {
        "none": {
            "CONFIG_LTO_NONE": "y",
            "CONFIG_LTO_CLANG_FULL": "n",
            "CONFIG_LTO_CLANG_THIN": "n",
            "CONFIG_DEBUG_INFO_BTF": "y",
            "CONFIG_DEBUG_INFO_BTF_MODULES": "y",
        },
        "thin": {
            "CONFIG_LTO_NONE": "n",
            "CONFIG_LTO_CLANG_FULL": "n",
            "CONFIG_LTO_CLANG_THIN": "y",
            "CONFIG_DEBUG_INFO_BTF": "n",
            "CONFIG_DEBUG_INFO_BTF_MODULES": "n",
        },
        "full": {
            "CONFIG_LTO_NONE": "n",
            "CONFIG_LTO_CLANG_FULL": "y",
            "CONFIG_LTO_CLANG_THIN": "n",
            "CONFIG_DEBUG_INFO_BTF": "n",
            "CONFIG_DEBUG_INFO_BTF_MODULES": "n",
        },
    }
    if lto_mode not in lto_required:
        fail(f"unknown LTO mode {lto_mode!r}")
    try:
        abi = identity["abi"]
        expected_krel = identity["kernel_releases"][flavor]
        package_version = identity["package_version"]
        publication_epoch = identity["publication_source_date_epoch"]
        identity_lto_mode = identity["lto_mode"]
        versioned_names = identity["package_names"]["versioned"]
        meta_names = identity["package_names"]["meta"]
    except (KeyError, TypeError) as exc:
        fail(f"malformed publication identity: {exc}")
    if (
        not isinstance(abi, str)
        or not isinstance(expected_krel, str)
        or not isinstance(package_version, str)
        or not isinstance(publication_epoch, int)
        or publication_epoch < 1
        or identity_lto_mode != lto_mode
        or not isinstance(versioned_names, list)
        or not isinstance(meta_names, list)
        or not all(isinstance(name, str) for name in versioned_names + meta_names)
    ):
        fail("publication identity contains invalid field types")
    expected_packages = {
        name for name in versioned_names if name.endswith(f"-{flavor}-amd64")
    }
    expected_packages.update(
        name for name in meta_names if name.endswith(f"-{flavor}-amd64")
    )
    expected_packages.update(
        name
        for name in versioned_names
        if name.endswith("-common") or "-kbuild-" in name
    )
    output.mkdir(parents=True, exist_ok=True)
    readelf_tool = f"llvm-readelf-{llvm_major}"

    if replay_evidence is None:
        build_dirs = list(source.glob(f"debian/build/build_amd64_none_{flavor}-amd64"))
        if len(build_dirs) != 1:
            fail(f"expected one amd64 build directory, found {build_dirs}")
        build: pathlib.Path | None = build_dirs[0]
        config_path = build / ".config"
        vmlinux = build / "vmlinux"
        replay_root = output / "attestation-replay"
    else:
        build = None
        replay_root = replay_evidence / "attestation-replay"
        config_path = replay_root / "config"
        # In replay mode the first positional argument is the verified,
        # decompressed replay ELF rather than a discarded source tree.
        vmlinux = source
    if not config_path.is_file() or not vmlinux.is_file():
        fail("final .config or vmlinux is missing")

    config = parse_config(config_path)
    raw_build_inputs = identity.get("build_inputs")
    if not isinstance(raw_build_inputs, dict):
        fail("publication identity has no build-input inventory")
    raw_flavor_inputs = raw_build_inputs.get("flavors")
    if (
        not isinstance(raw_flavor_inputs, list)
        or len(raw_flavor_inputs) != 3
        or not all(
            isinstance(item, dict) and isinstance(item.get("flavor"), str)
            for item in raw_flavor_inputs
        )
    ):
        fail("publication identity has no flavor configuration inventory")
    flavor_inputs = {
        item.get("flavor"): item
        for item in raw_flavor_inputs
        if isinstance(item, dict)
    }
    expected_config_digest = flavor_inputs.get(flavor, {}).get("config_sha256")
    if (
        len(flavor_inputs) != 3
        or set(flavor_inputs) != {"v2", "v3", "v4"}
        or not isinstance(expected_config_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_config_digest)
    ):
        fail("publication identity has a malformed flavor configuration inventory")
    actual_config_digest = policy_config_digest(config)
    if actual_config_digest != expected_config_digest:
        fail(
            "final configuration differs from the normalized pre-identity "
            f"{flavor} policy"
        )
    required = {
        "CONFIG_CC_IS_CLANG": "y",
        "CONFIG_CC_IS_GCC": "n",
        "CONFIG_AS_IS_LLVM": "y",
        "CONFIG_AS_IS_GNU": "n",
        "CONFIG_LD_IS_LLD": "y",
        "CONFIG_LD_IS_BFD": "n",
        "CONFIG_RUST": "y",
        "CONFIG_DEBUG_INFO_NONE": "n",
        "CONFIG_MODULE_SIG": "n",
        "CONFIG_SECURITY_LOCKDOWN_LSM": "n",
        "CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT": "n",
        **lto_required[lto_mode],
    }
    for key, expected in required.items():
        actual = config.get(key, "n" if expected == "n" else "<missing>")
        if actual != expected:
            fail(f"{key}={actual!r}, expected {expected!r}")
    if config.get("CONFIG_BUILD_SALT") != f'"{expected_krel}"':
        fail("final CONFIG_BUILD_SALT does not equal the publication KREL")
    for key in ("CONFIG_CLANG_VERSION", "CONFIG_LLD_VERSION"):
        value = int(config.get(key, "0"))
        if value // 10000 != llvm_major:
            fail(f"{key}={value} does not identify major {llvm_major}")
    for key in (
        "CONFIG_CC_VERSION_TEXT",
        "CONFIG_RUSTC_VERSION",
        "CONFIG_RUSTC_LLVM_VERSION",
        "CONFIG_RUSTC_VERSION_TEXT",
        "CONFIG_PAHOLE_VERSION",
    ):
        if not config.get(key) or config[key] in ("0", '""'):
            fail(f"{key} is absent from the final configuration")

    capture_path = replay_root / "build-tree-inventory.json"
    try:
        captured_build = json.loads(capture_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read captured build-tree inventory: {exc}")
    captured_module_targets = captured_build.get("module_targets")
    captured_private_keys = captured_build.get("possible_private_key_files")
    captured_public_certificates = captured_build.get("public_x509_files")
    if (
        captured_build.get("schema_version") != 1
        or captured_build.get("status") != "COMPLETE"
        or captured_build.get("llvm_major") != llvm_major
        or not isinstance(captured_module_targets, list)
        or len(captured_module_targets) < 1000
        or captured_module_targets != sorted(set(captured_module_targets))
        or not all(isinstance(item, str) for item in captured_module_targets)
        or captured_private_keys != []
        or not isinstance(captured_public_certificates, list)
        or captured_public_certificates != sorted(set(captured_public_certificates))
        or not all(isinstance(item, str) for item in captured_public_certificates)
    ):
        fail("captured build-tree inventory is incomplete or malformed")
    captured_module_target_set = set(captured_module_targets)

    debs = sorted(artifacts.glob("*.deb"))
    ddebs = sorted(artifacts.glob("*.ddeb"))
    udebs = sorted(artifacts.glob("*.udeb"))
    buildinfos = sorted(artifacts.glob("*.buildinfo"))
    changes = sorted(artifacts.glob("*.changes"))
    if not debs or len(buildinfos) != 1 or len(changes) != 1:
        fail(
            "expected packages, one .buildinfo, and one .changes; got "
            f"{len(debs)}, {len(buildinfos)}, and {len(changes)}"
        )
    if ddebs:
        fail(f"unpublished automatic debug packages were built: {[path.name for path in ddebs]}")
    if udebs:
        fail(f"unpublished installer packages were built: {[path.name for path in udebs]}")

    with tempfile.TemporaryDirectory(prefix="dkc-attest-", dir="/work") as temp_name:
        temporary = pathlib.Path(temp_name)
        extracted = temporary / "packages"
        extracted.mkdir()
        package_fields: dict[str, dict[str, str]] = {}
        for deb in debs:
            fields_text = subprocess.check_output(
                [
                    "dpkg-deb",
                    "--showformat=${Package}\t${Version}\t${source:Package}\t"
                    "${source:Version}\t${Architecture}\t${Depends}\t${Pre-Depends}\t"
                    "${Recommends}\t${Suggests}\n",
                    "--show",
                    deb,
                ],
                text=True,
            ).rstrip("\n").split("\t")
            if len(fields_text) != 9:
                fail(f"{deb.name} produced a malformed binary metadata record")
            package = fields_text[0]
            if fields_text[1:4] != [
                package_version,
                "dkc-linux",
                package_version,
            ]:
                fail(f"{package} has unexpected binary/source identity: {fields_text[1:4]}")
            if package.endswith("-dbg"):
                fail(f"unpublished kernel debug package was built: {package}")
            if package in package_fields:
                fail(f"duplicate binary package identity in one flavor: {package}")
            expected_architecture = (
                "all" if package == f"dkc-linux-headers-{abi}-common" else "amd64"
            )
            if fields_text[4] != expected_architecture:
                fail(
                    f"{package} Architecture={fields_text[4]!r}, "
                    f"expected {expected_architecture!r}"
                )
            package_fields[package] = {
                "depends": fields_text[5],
                "pre_depends": fields_text[6],
                "recommends": fields_text[7],
                "suggests": fields_text[8],
            }
            subprocess.run(["dpkg-deb", "--extract", deb, extracted], check=True)

        if set(package_fields) != expected_packages:
            fail(
                "binary package set differs from the publication inventory: "
                f"missing={sorted(expected_packages - set(package_fields))}, "
                f"unexpected={sorted(set(package_fields) - expected_packages)}"
            )
        foreign_names = sorted(name for name in package_fields if not name.startswith("dkc-linux-"))
        if foreign_names:
            fail(f"non-DKC product package names escaped into the build: {foreign_names}")

        header_root = f"usr/src/linux-headers-{expected_krel}"
        common_root = f"usr/src/linux-headers-{abi}-common"
        if any((extracted / "usr/src").glob("dkc-linux-headers-*")):
            fail("renamed binary package leaked into the conventional /usr/src header ABI")
        for relative in (
            f"{header_root}/.kernelvariables",
            f"{header_root}/Makefile",
            f"{header_root}/Module.symvers",
            f"{header_root}/include/generated/autoconf.h",
        ):
            if not (extracted / relative).is_file():
                fail(f"headers package lacks required payload: {relative}")
        validate_kernelvariables(
            extracted / header_root / ".kernelvariables", llvm_major
        )
        require_relative_symlink(
            extracted,
            f"usr/lib/modules/{expected_krel}/build",
            header_root,
        )
        require_relative_symlink(
            extracted,
            f"usr/lib/modules/{expected_krel}/source",
            common_root,
        )
        for leaf in ("scripts", "tools"):
            require_relative_symlink(
                extracted,
                f"{header_root}/{leaf}",
                f"usr/lib/linux-kbuild-{abi}/{leaf}",
            )
        kbuild_root = f"usr/lib/linux-kbuild-{abi}"
        if any((extracted / "usr/lib").glob("dkc-linux-kbuild-*")) or any(
            (extracted / "usr/src").glob("dkc-linux-kbuild-*")
        ):
            fail("renamed package leaked into the conventional Kbuild filesystem ABI")
        for relative in (
            f"{kbuild_root}/scripts/basic/fixdep",
            f"{kbuild_root}/tools/objtool/objtool",
        ):
            if not (extracted / relative).is_file():
                fail(f"Kbuild package lacks required payload: {relative}")
        require_relative_symlink(
            extracted,
            f"usr/src/linux-kbuild-{abi}",
            kbuild_root,
        )
        if not (extracted / common_root / "Makefile").is_file():
            fail(f"common headers package lacks {common_root}/Makefile")
        for leaf in ("scripts", "tools"):
            require_relative_symlink(
                extracted,
                f"{common_root}/{leaf}",
                f"usr/lib/linux-kbuild-{abi}/{leaf}",
            )

        shipped_configs = sorted((extracted / "boot").glob("config-*"))
        if len(shipped_configs) != 1:
            fail(f"expected one shipped config, found {shipped_configs}")
        redacted = re.compile(
            r"CONFIG_(?:MODULE_SIG_(?:ALL|KEY)|SYSTEM_TRUSTED_KEYS|BUILD_SALT)[ =]"
        )
        normalized_build = "\n".join(
            line
            for line in config_path.read_text(encoding="utf-8").splitlines()
            if not redacted.search(line)
        ) + "\n"
        shipped_text = shipped_configs[0].read_text(encoding="utf-8")
        if shipped_configs[0].name != f"config-{expected_krel}":
            fail(f"shipped config does not carry expected KREL {expected_krel}")
        if shipped_text != normalized_build:
            fail("shipped /boot config has changes beyond Debian's reviewed redactions")
        shipped_config_sha256 = sha256(shipped_configs[0])
        shipped_config = parse_config(shipped_configs[0])
        for key, expected in required.items():
            actual = shipped_config.get(key, "n" if expected == "n" else "<missing>")
            if actual != expected:
                fail(f"shipped config {key}={actual!r}, expected {expected!r}")

        for package, fields in package_fields.items():
            relation_text = ", ".join(fields.values())
            compiler_dependencies = {
                name
                for name in dependency_names(relation_text)
                if re.fullmatch(r"(?:clang|lld|llvm)-[0-9]+", name)
            }
            if re.fullmatch(r"dkc-linux-headers-[0-9].*-amd64", package):
                expected = {f"clang-{llvm_major}", f"lld-{llvm_major}", f"llvm-{llvm_major}"}
                if compiler_dependencies != expected:
                    fail(f"{package} compiler dependencies {compiler_dependencies} != {expected}")
            elif compiler_dependencies:
                fail(f"{package} unexpectedly carries compiler dependencies {compiler_dependencies}")

        module_paths = sorted(
            path
            for path in extracted.rglob("*.ko*")
            if path.name.endswith((".ko", ".ko.xz", ".ko.zst"))
        )
        if not module_paths:
            fail("no shipped modules found")
        modules: dict[str, pathlib.Path] = {}
        for module in module_paths:
            module_name = module_identity(module, extracted)
            if module_name in modules:
                fail(f"duplicate shipped module identity: {module_name}")
            modules[module_name] = module
        wrong_module_roots = [
            str(path.relative_to(extracted))
            for path in module_paths
            if f"/lib/modules/{expected_krel}/" not in f"/{path.relative_to(extracted)}"
        ]
        if wrong_module_roots:
            fail(f"modules escaped the expected KREL directory: {wrong_module_roots[:5]}")

        btf_required = lto_mode == "none"
        btf_policy = "required" if btf_required else "forbidden"
        inspect_elf(readelf_tool, vmlinux, "vmlinux", llvm_major, True)
        (require_btf if btf_required else forbid_btf)(
            readelf_tool, vmlinux, "vmlinux"
        )
        sampled_modules = select_module_sample(modules)
        for identity in sampled_modules:
            materialized = materialize_module(modules[identity], temporary)
            inspect_elf(readelf_tool, materialized, identity, llvm_major, False)
            (require_btf if btf_required else forbid_btf)(
                readelf_tool, materialized, identity
            )
            if materialized != modules[identity]:
                materialized.unlink()

        header_vmlinux = sorted(extracted.glob("usr/src/linux-headers-*/vmlinux"))
        if btf_required and not header_vmlinux:
            fail("headers package contains no BTF vmlinux")
        if not btf_required and header_vmlinux:
            fail("headers package contains a vmlinux payload while BTF is disabled")
        for path in header_vmlinux:
            if path.parent.name != f"linux-headers-{expected_krel}":
                fail(f"header BTF vmlinux has unexpected KREL path: {path}")
            require_btf(readelf_tool, path, str(path.relative_to(extracted)))
        header_btf_paths = [str(path.relative_to(extracted)) for path in header_vmlinux]

    kbuild_inventory = (
        replay_evidence if replay_evidence is not None else output
    ) / "kbuild-commands.tsv.xz"
    records, record_count, tool_counts, normalized_inventory = load_kbuild_inventory(
        kbuild_inventory, llvm_major
    )
    if (
        captured_build.get("kbuild_cmd_records") != record_count
        or captured_build.get("kbuild_command_inventory_sha256")
        != text_sha256(normalized_inventory)
    ):
        fail("captured Kbuild inventory differs from its build-tree record")
    for required_tool in (f"clang-{llvm_major}", f"ld.lld-{llvm_major}"):
        if not tool_counts[required_tool]:
            fail(f"Kbuild command inventory never invoked {required_tool}")

    for identity in sorted(modules):
        expected_cmd = pathlib.PurePosixPath(identity).parent / (
            f".{pathlib.PurePosixPath(identity).name}.cmd"
        )
        if identity not in captured_module_target_set:
            fail(f"shipped module lacks its captured build-tree target: {identity}")
        candidates = records.get(identity, [])
        matching = [record for record in candidates if record[0] == expected_cmd]
        if not matching:
            fail(f"shipped module lacks its final Kbuild command: {identity}")
        if len(matching) != 1:
            fail(f"shipped module has ambiguous final Kbuild commands: {identity}")
        _cmd_path, _command, tools = matching[0]
        if f"ld.lld-{llvm_major}" not in tools:
            fail(f"module final link did not use ld.lld-{llvm_major}: {identity}")

    if replay_evidence is not None:
        shutil.copyfile(kbuild_inventory, output / "kbuild-commands.tsv.xz")

    buildinfo_fields = parse_deb822(buildinfos[0])
    for key, expected in (
        ("Source", "dkc-linux"),
        ("Version", package_version),
        ("Build-Architecture", "amd64"),
    ):
        if buildinfo_fields.get(key) != expected:
            fail(f".buildinfo {key}={buildinfo_fields.get(key)!r}, expected {expected!r}")
    if timestamp_epoch(buildinfo_fields.get("Build-Date", ""), "Build-Date") < publication_epoch:
        fail(".buildinfo Build-Date predates the publication changelog")
    expected_architectures = {"amd64", "all"}
    buildinfo_architectures = set(buildinfo_fields.get("Architecture", "").split())
    if buildinfo_architectures != expected_architectures:
        fail(
            ".buildinfo architectures differ: "
            f"{sorted(buildinfo_architectures)} != {sorted(expected_architectures)}"
        )
    if binary_names(buildinfo_fields.get("Binary", "")) != expected_packages:
        fail(".buildinfo Binary field differs from the selected package set")
    require_checksum_set(
        checksum_records(buildinfo_fields.get("Checksums-Sha256", ""), ".buildinfo"),
        debs,
        ".buildinfo",
    )
    installed = buildinfo_fields.get("Installed-Build-Depends", "")
    for package in (f"clang-{llvm_major}", f"lld-{llvm_major}", f"llvm-{llvm_major}", "rustc", "bindgen"):
        if not re.search(rf"(?:^|\n){re.escape(package)} \(= [^)]+\)(?:,|$)", installed):
            fail(f".buildinfo lacks exact Installed-Build-Depends entry for {package}")
    for match in re.finditer(r"(?:^|\n)(?:clang|lld|llvm)-(\d+) ", installed):
        if int(match.group(1)) != llvm_major:
            fail(".buildinfo names an unexpected LLVM compiler/tool major")
    if re.search(r"(?:^|\n)gcc-15 ", installed):
        fail(".buildinfo names forbidden gcc-15")

    changes_fields = parse_deb822(changes[0])
    for key, expected in (("Source", "dkc-linux"), ("Version", package_version)):
        if changes_fields.get(key) != expected:
            fail(f".changes {key}={changes_fields.get(key)!r}, expected {expected!r}")
    if changes_fields.get("Distribution") != "trixie":
        fail(".changes does not target the trixie suite")
    if timestamp_epoch(changes_fields.get("Date", ""), "Date") < publication_epoch:
        fail(".changes Date predates the publication changelog")
    if binary_names(changes_fields.get("Binary", "")) != expected_packages:
        fail(".changes Binary field differs from the selected package set")
    changes_architectures = set(changes_fields.get("Architecture", "").split())
    if changes_architectures != expected_architectures:
        fail(
            ".changes architectures differ: "
            f"{sorted(changes_architectures)} != {sorted(expected_architectures)}"
        )
    require_checksum_set(
        checksum_records(changes_fields.get("Checksums-Sha256", ""), ".changes"),
        [*debs, buildinfos[0]],
        ".changes",
    )

    if replay_evidence is None:
        attested_vmlinux_sha256 = sha256(vmlinux)
    else:
        try:
            replay_manifest = json.loads(
                (replay_root / "manifest.json").read_text(encoding="utf-8")
            )
            attested_vmlinux_sha256 = replay_manifest["original_vmlinux"]["sha256"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            fail(f"cannot identify original vmlinux from replay manifest: {exc}")
        if not isinstance(attested_vmlinux_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", attested_vmlinux_sha256
        ):
            fail("replay manifest has an invalid original vmlinux digest")

    report = {
        "schema_version": 2,
        "status": "PASS",
        "llvm_major": llvm_major,
        "lto_mode": lto_mode,
        "flavor": flavor,
        "kernel_release": expected_krel,
        "config_sha256": sha256(config_path),
        "normalized_policy_config_sha256": actual_config_digest,
        "shipped_config_sha256": shipped_config_sha256,
        "vmlinux_sha256": attested_vmlinux_sha256,
        "btf_policy": btf_policy,
        "btf_preserved": btf_required,
        "shipped_modules": len(modules),
        "module_commands_reconciled": len(modules),
        "sampled_module_elfs": sampled_modules,
        "header_btf_vmlinux": header_btf_paths,
        "header_root": header_root,
        "header_source_root": common_root,
        "header_links": "PASS",
        "header_tool_assignments": "PASS",
        "kbuild_cmd_files": captured_build["kbuild_cmd_files"],
        "kbuild_cmd_targets": len(records),
        "kbuild_cmd_records": record_count,
        "kbuild_command_inventory_sha256": text_sha256(normalized_inventory),
        "tool_invocations": dict(sorted(tool_counts.items())),
        "public_x509_files": captured_public_certificates,
        "packages": {path.name: sha256(path) for path in debs},
        "buildinfo": buildinfos[0].name,
        "changes": changes[0].name,
    }
    (output / "attestation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output / "package-fields.json").write_text(
        json.dumps(package_fields, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"attestation PASS: {len(modules)} module commands, "
        f"{len(sampled_modules)} sampled ELFs, {record_count} Kbuild records, "
        f"{len(debs)} packages",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
