#!/usr/bin/env python3
"""Stream-disassemble vmlinux and every shipped module for SIMD/FPU use.

The disassembler output is never retained.  Only exact artifact/symbol (or the
two reviewed alternative-code sections) matches reach the bounded JSON report.
"""

from __future__ import annotations

import argparse
import bisect
import json
import lzma
import pathlib
import re
import shutil
import subprocess
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass


REGISTER = re.compile(
    r"%(?:(?:x|y|z)mm\d+|mm\d+|k[0-7]|tmm[0-7]|bnd[0-3]|st(?:\(\d+\))?)\b"
)

# Most SIMD/FPU instructions name an architectural register and are caught by
# REGISTER.  These families also have forms whose operands are only memory, or
# no operands at all.  Missing them would let compiler-generated x87 or state
# manipulation escape merely because llvm-objdump printed no %xmm/%st token.
IMPLICIT = {
    "clts",
    "emms",
    "femms",
    "ldmxcsr",
    "ldtilecfg",
    "stmxcsr",
    "sttilecfg",
    "tilerelease",
    "vzeroall",
    "vzeroupper",
    "wait",
}
X87 = re.compile(
    r"^f(?:2xm1|abs|add|bld|bstp|chs|clex|cmov|com|cos|decstp|div|free|"
    r"disi|eni|iadd|icom|idiv|ild|imul|incstp|init|ist|isub|ld|mul|"
    r"nclex|ninit|nop|nsave|nst|"
    r"patan|prem|prem1|ptan|rndint|rstor|save|scale|sin|sincos|sqrt|st|sub|"
    r"setpm|tst|ucom|wait|xam|xch|xtract|yl2x|yl2xp1)"
)
EXTENDED_STATE = re.compile(
    r"^(?:fxsave|fxrstor|xsave|xsavec|xsaveopt|xsaves|xrstor|xrstors)(?:64)?$"
)
SECTION = re.compile(r"^Disassembly of section ([^:]+):$")
SYMBOL = re.compile(r"^([0-9a-fA-F]+) <([^>]+)>:$")
INSTRUCTION = re.compile(r"^\s*([0-9a-fA-F]+):\s+([^\s]+)(?:\s+(.*))?$")
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_+.,@~-]+$")
THIN_LTO_INTERNAL_SUFFIX = re.compile(r"^(?P<base>.+)\.llvm\.[0-9]+$")
FULL_LTO_INTERNAL_SUFFIX = re.compile(r"^(?P<base>.+)\.[0-9]+$")
SOURCE_VERSION = "7.1.7-1"


@dataclass(frozen=True)
class AllowEntry:
    artifact: str
    selector_kind: str
    selector: str
    reason: str
    lto_modes: tuple[str, ...] = ("none", "thin", "full")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.artifact, self.selector_kind, self.selector


class SymbolMap:
    def __init__(self, path: pathlib.Path | None):
        self.addresses: list[int] = []
        self.names: list[str] = []
        self.types: list[str] = []
        if path is None:
            return
        for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
            fields = line.split(maxsplit=2)
            if len(fields) != 3 or not re.fullmatch(r"[0-9a-fA-F]+", fields[0]):
                continue
            address = int(fields[0], 16)
            symbol_type = fields[1]
            # Prefer a text/weak symbol when aliases share one address.  This
            # avoids attributing code to an absolute/data alias listed later.
            if self.addresses and self.addresses[-1] == address:
                if self.types[-1] not in "tTwW" and symbol_type in "tTwW":
                    self.names[-1] = fields[2]
                    self.types[-1] = symbol_type
                continue
            self.addresses.append(address)
            self.names.append(fields[2])
            self.types.append(symbol_type)
        if not self.addresses or self.addresses != sorted(self.addresses):
            raise SystemExit("System.map is empty or not address-sorted")

    def lookup(self, address: int) -> tuple[str, str] | None:
        index = bisect.bisect_right(self.addresses, address) - 1
        return (self.names[index], self.types[index]) if index >= 0 else None


def derive_fpu_symbols(
    build_root: pathlib.Path,
    policy_path: pathlib.Path,
    llvm_major: int,
) -> tuple[dict[tuple[str, str, str], AllowEntry], int]:
    raw = tomllib.loads(policy_path.read_text(encoding="utf-8"))
    if set(raw) != {"schema_version", "source_version", "final_artifact", "objects"}:
        raise SystemExit("intentional FPU object policy has unexpected fields")
    if raw["schema_version"] != 1 or raw["source_version"] != SOURCE_VERSION:
        raise SystemExit("intentional FPU object policy is not pinned to linux 7.1.7-1")
    artifact = raw["final_artifact"]
    objects = raw["objects"]
    if artifact != "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko":
        raise SystemExit("intentional FPU policy names an unreviewed final artifact")
    if not isinstance(objects, list) or len(objects) != 66 or len(objects) != len(set(objects)):
        raise SystemExit("intentional FPU policy must identify exactly 66 unique objects")

    tool = f"llvm-nm-{llvm_major}"
    result: dict[tuple[str, str, str], AllowEntry] = {}
    for relative in objects:
        if not isinstance(relative, str) or relative.startswith("/") or ".." in pathlib.PurePosixPath(relative).parts:
            raise SystemExit(f"unsafe intentional FPU object: {relative!r}")
        path = build_root / relative
        if not path.is_file():
            raise SystemExit(f"reviewed intentional FPU object is absent from the build: {relative}")
        output = subprocess.check_output(
            [tool, "--defined-only", "--format=posix", path], text=True, encoding="utf-8"
        )
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 2 and len(fields[1]) == 1 and fields[1] in "tTwW":
                entry = AllowEntry(
                    artifact,
                    "symbol",
                    fields[0],
                    f"defined by reviewed CC_FLAGS_FPU object {relative}",
                )
                result[entry.key] = entry
    if len(result) < 500:
        raise SystemExit(f"derived FPU symbol inventory is unexpectedly small: {len(result)}")
    return result, len(objects)


def write_derived_fpu_inventory(
    path: pathlib.Path,
    entries: dict[tuple[str, str, str], AllowEntry],
    object_count: int,
    llvm_major: int,
) -> None:
    records = [
        {
            "artifact": entry.artifact,
            "symbol": entry.selector,
            "reason": entry.reason,
        }
        for entry in sorted(entries.values(), key=lambda item: item.key)
    ]
    write_json(
        path,
        {
            "schema_version": 1,
            "status": "COMPLETE",
            "source_version": SOURCE_VERSION,
            "llvm_major": llvm_major,
            "object_count": object_count,
            "symbols": records,
        },
    )


def load_derived_fpu_inventory(
    path: pathlib.Path, llvm_major: int
) -> tuple[dict[tuple[str, str, str], AllowEntry], int]:
    raw = read_json(path)
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "status",
        "source_version",
        "llvm_major",
        "object_count",
        "symbols",
    }:
        raise SystemExit("derived FPU inventory has unexpected fields")
    if (
        raw["schema_version"] != 1
        or raw["status"] != "COMPLETE"
        or raw["source_version"] != SOURCE_VERSION
        or raw["llvm_major"] != llvm_major
        or raw["object_count"] != 66
        or not isinstance(raw["symbols"], list)
    ):
        raise SystemExit("derived FPU inventory identity is invalid")
    entries: dict[tuple[str, str, str], AllowEntry] = {}
    for record in raw["symbols"]:
        if not isinstance(record, dict) or set(record) != {"artifact", "symbol", "reason"}:
            raise SystemExit("derived FPU inventory contains a malformed symbol")
        artifact, symbol, reason = record["artifact"], record["symbol"], record["reason"]
        if not all(isinstance(value, str) and value for value in (artifact, symbol, reason)):
            raise SystemExit("derived FPU inventory contains an empty symbol field")
        entry = AllowEntry(artifact, "symbol", symbol, reason)
        if entry.key in entries:
            raise SystemExit(f"duplicate derived FPU symbol: {entry.key}")
        entries[entry.key] = entry
    if len(entries) < 500:
        raise SystemExit("derived FPU symbol inventory is unexpectedly small")
    if list(entries) != sorted(entries):
        raise SystemExit("derived FPU symbol inventory is not sorted")
    return entries, raw["object_count"]


def load_allowlist(
    path: pathlib.Path, lto_mode: str | None = None
) -> tuple[str, dict[tuple[str, str, str], AllowEntry]]:
    if lto_mode not in {None, "none", "thin", "full"}:
        raise SystemExit(f"invalid SIMD policy LTO mode: {lto_mode!r}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    if set(raw) != {"schema_version", "source_version", "entry"}:
        raise SystemExit("SIMD allowlist has unexpected top-level fields")
    if raw["schema_version"] != 1 or raw["source_version"] != SOURCE_VERSION:
        raise SystemExit("SIMD allowlist is not pinned to linux 7.1.7-1")
    result: dict[tuple[str, str, str], AllowEntry] = {}
    ordered_keys: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    entries = raw["entry"]
    if not isinstance(entries, list):
        raise SystemExit("SIMD allowlist entry must be an array of tables")
    for item in entries:
        if not isinstance(item, dict) or set(item) not in (
            {"artifact", "symbol", "reason"},
            {"artifact", "section", "reason"},
            {"artifact", "symbol", "reason", "lto_modes"},
            {"artifact", "section", "reason", "lto_modes"},
        ):
            raise SystemExit(f"malformed SIMD allowlist entry: {item!r}")
        kind = "symbol" if "symbol" in item else "section"
        artifact, selector, reason = item["artifact"], item[kind], item["reason"]
        if not all(isinstance(value, str) and value for value in (artifact, selector, reason)):
            raise SystemExit("SIMD allowlist entries require non-empty strings")
        if artifact.startswith("/") or ".." in pathlib.PurePosixPath(artifact).parts:
            raise SystemExit(f"unsafe SIMD artifact path: {artifact!r}")
        if any(character in selector for character in "*?[]\n\r") or selector.strip() != selector:
            raise SystemExit(f"SIMD selector is not exact and safe: {selector!r}")
        modes = item.get("lto_modes", ["none", "thin", "full"])
        canonical_modes = ("none", "thin", "full")
        if (
            not isinstance(modes, list)
            or not modes
            or any(not isinstance(mode, str) or mode not in canonical_modes for mode in modes)
            or len(modes) != len(set(modes))
            or modes != [mode for mode in canonical_modes if mode in modes]
        ):
            raise SystemExit(f"invalid SIMD allowlist LTO modes: {modes!r}")
        entry = AllowEntry(artifact, kind, selector, reason, tuple(modes))
        if entry.key in seen:
            raise SystemExit(f"duplicate SIMD allowlist entry: {entry.key}")
        seen.add(entry.key)
        if lto_mode is None or lto_mode in entry.lto_modes:
            result[entry.key] = entry
        ordered_keys.append(entry.key)
    if ordered_keys != sorted(ordered_keys):
        raise SystemExit("SIMD allowlist entries must be sorted by artifact and selector")
    return raw["source_version"], result


def materialize_module(path: pathlib.Path, work: pathlib.Path) -> pathlib.Path:
    name = path.name
    while pathlib.Path(name).suffix in (".xz", ".zst"):
        name = pathlib.Path(name).stem
    if not name.endswith(".ko") or not all(
        SAFE_COMPONENT.fullmatch(part) for part in pathlib.PurePosixPath(name).parts
    ):
        raise SystemExit(f"unsafe or non-module artifact name: {path}")
    target = work / name
    if path.suffix == ".xz":
        with lzma.open(path, "rb") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, 1024 * 1024)
    elif path.suffix == ".zst":
        with target.open("wb") as output:
            subprocess.run(["zstd", "-q", "-d", "-c", path], stdout=output, check=True)
    else:
        shutil.copyfile(path, target)
    return target


def module_identity(path: pathlib.Path) -> str:
    parts = path.parts
    try:
        index = parts.index("modules")
    except ValueError as exc:
        raise SystemExit(f"module is outside lib/modules: {path}") from exc
    if len(parts) <= index + 2:
        raise SystemExit(f"module path lacks a KREL and payload path: {path}")
    relative = pathlib.PurePosixPath(*parts[index + 2 :]).as_posix()
    for suffix in (".xz", ".zst"):
        if relative.endswith(suffix):
            relative = relative[: -len(suffix)]
    return relative


def scan_binary(
    tool: str,
    path: pathlib.Path,
    artifact: str,
    symbol_map: SymbolMap,
    observed: Counter[tuple[str, str, str]],
) -> tuple[int, int]:
    process = subprocess.Popen(
        [tool, "--disassemble", "--no-show-raw-insn", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    section = "<unknown-section>"
    symbol: str | None = None
    instructions = matches = 0
    for raw in process.stdout:
        line = raw.rstrip("\n")
        if found := SECTION.fullmatch(line):
            section = found.group(1)
            symbol = None
            continue
        if found := SYMBOL.fullmatch(line):
            # llvm-objdump may print labels inside one function (for example
            # assembly local labels) as additional headers.  System.map is the
            # authoritative function/symbol attribution for stripped vmlinux;
            # module ELFs keep their ordinary symbol-table names.
            symbol = None if symbol_map.addresses else found.group(2)
            continue
        found = INSTRUCTION.fullmatch(line)
        if not found:
            continue
        # The x86 kernel linker marks the aggregate vmlinux .data section WAX,
        # although the kernel maps that range NX and System.map identifies its
        # contents as data.  llvm-objdump therefore decodes arbitrary tables as
        # fake instructions.  It is an executable *flag* anomaly, not an
        # executable-code section; keep this exact exclusion visible here.
        if artifact == "vmlinux" and section == ".data":
            continue
        instructions += 1
        mnemonic = found.group(2).lower()
        operands = found.group(3) or ""
        if (
            not REGISTER.search(operands)
            and mnemonic not in IMPLICIT
            and X87.match(mnemonic) is None
            and EXTENDED_STATE.fullmatch(mnemonic) is None
        ):
            continue
        matches += 1
        address = int(found.group(1), 16)
        mapped = symbol_map.lookup(address)
        alternative_section = artifact == "vmlinux" and section in {
            ".altinstr_aux",
            ".altinstr_replacement",
        }
        if alternative_section:
            section_key = (artifact, "section", section)
            observed[section_key] += 1
            continue
        else:
            resolved_symbol = mapped[0] if mapped is not None else symbol or "<unknown-symbol>"
        symbol_key = (artifact, "symbol", resolved_symbol)
        observed[symbol_key] += 1
    stderr = process.stderr.read() if process.stderr is not None else ""
    returncode = process.wait()
    if returncode:
        raise SystemExit(
            f"{tool} failed for {artifact} with rc={returncode}: {stderr[-1000:]}"
        )
    return instructions, matches


def write_json(path: pathlib.Path, value: object) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    if path.suffix == ".xz":
        with lzma.open(path, "wt", encoding="utf-8", preset=1) as stream:
            stream.write(payload)
    else:
        path.write_text(payload, encoding="utf-8")


def read_json(path: pathlib.Path) -> object:
    if path.suffix == ".xz":
        with lzma.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream)
    return json.loads(path.read_text(encoding="utf-8"))


def reviewed_key(
    key: tuple[str, str, str],
    allowlist: dict[tuple[str, str, str], AllowEntry],
    full_lto_alias_keys: set[tuple[str, str, str]] | frozenset[tuple[str, str, str]] = frozenset(),
) -> tuple[str, str, str] | None:
    if key in allowlist:
        return key
    artifact, kind, selector = key
    if kind != "symbol":
        return None
    if match := THIN_LTO_INTERNAL_SUFFIX.fullmatch(selector):
        candidate = (artifact, kind, match.group("base"))
        return candidate if candidate in allowlist else None
    if match := FULL_LTO_INTERNAL_SUFFIX.fullmatch(selector):
        candidate = (artifact, kind, match.group("base"))
        # Full LTO appends numeric identifiers while merging translation-unit
        # local symbols.  Accept that spelling only for the exact symbol set
        # derived from reviewed CC_FLAGS_FPU objects.  Manually reviewed final
        # symbols remain exact, so a coincidental dotted name cannot inherit
        # their policy entry.
        return candidate if candidate in full_lto_alias_keys else None
    return None


def evaluate_observations(
    observed: Counter[tuple[str, str, str]],
    allowlist: dict[tuple[str, str, str], AllowEntry],
    derived_keys: set[tuple[str, str, str]],
) -> tuple[
    Counter[tuple[str, str, str]],
    Counter[tuple[str, str, str]],
    list[tuple[str, str, str]],
    list[dict[str, str]],
]:
    used: Counter[tuple[str, str, str]] = Counter()
    unexpected: Counter[tuple[str, str, str]] = Counter()
    aliases: list[dict[str, str]] = []
    for key, count in observed.items():
        reviewed = reviewed_key(key, allowlist, derived_keys)
        if reviewed is None:
            unexpected[key] += count
            continue
        used[reviewed] += count
        if reviewed != key:
            aliases.append(
                {
                    "artifact": key[0],
                    "observed_symbol": key[2],
                    "reviewed_symbol": reviewed[2],
                }
            )
    unused = sorted(set(allowlist) - derived_keys - set(used))
    return used, unexpected, unused, sorted(
        aliases, key=lambda item: (item["artifact"], item["observed_symbol"])
    )


def observation_document(
    observed: Counter[tuple[str, str, str]],
    llvm_major: int,
    module_count: int,
    total_instructions: int,
    total_matches: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "COMPLETE",
        "llvm_major": llvm_major,
        "vmlinux_files": 1,
        "module_files": module_count,
        "decoded_instructions": total_instructions,
        "simd_fpu_instructions": total_matches,
        "observed": [
            {
                "artifact": artifact,
                "selector_kind": kind,
                "selector": selector,
                "instruction_count": count,
            }
            for (artifact, kind, selector), count in sorted(observed.items())
        ],
    }


def load_observations(
    path: pathlib.Path, llvm_major: int
) -> tuple[Counter[tuple[str, str, str]], int, int, int]:
    raw = read_json(path)
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "status",
        "llvm_major",
        "vmlinux_files",
        "module_files",
        "decoded_instructions",
        "simd_fpu_instructions",
        "observed",
    }:
        raise SystemExit("SIMD observation inventory has unexpected fields")
    if (
        raw["schema_version"] != 1
        or raw["status"] != "COMPLETE"
        or raw["llvm_major"] != llvm_major
        or raw["vmlinux_files"] != 1
        or not isinstance(raw["module_files"], int)
        or raw["module_files"] < 1000
        or not isinstance(raw["decoded_instructions"], int)
        or not isinstance(raw["simd_fpu_instructions"], int)
        or not isinstance(raw["observed"], list)
    ):
        raise SystemExit("SIMD observation inventory identity is invalid")
    observed: Counter[tuple[str, str, str]] = Counter()
    for record in raw["observed"]:
        if not isinstance(record, dict) or set(record) != {
            "artifact",
            "selector_kind",
            "selector",
            "instruction_count",
        }:
            raise SystemExit("SIMD observation inventory contains a malformed record")
        artifact = record["artifact"]
        kind = record["selector_kind"]
        selector = record["selector"]
        count = record["instruction_count"]
        if (
            not isinstance(artifact, str)
            or kind not in {"symbol", "section"}
            or not isinstance(selector, str)
            or not selector
            or not isinstance(count, int)
            or count < 1
        ):
            raise SystemExit("SIMD observation inventory contains invalid values")
        key = (artifact, kind, selector)
        if key in observed:
            raise SystemExit(f"duplicate SIMD observation: {key}")
        observed[key] = count
    if sum(observed.values()) != raw["simd_fpu_instructions"]:
        raise SystemExit("SIMD observation counts do not match their total")
    return (
        observed,
        raw["module_files"],
        raw["decoded_instructions"],
        raw["simd_fpu_instructions"],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("vmlinux", type=pathlib.Path)
    parser.add_argument("artifacts", type=pathlib.Path)
    parser.add_argument("allowlist", type=pathlib.Path)
    parser.add_argument("report", type=pathlib.Path)
    parser.add_argument("llvm_major", type=int)
    parser.add_argument("--lto-mode", choices=("none", "thin", "full"), required=True)
    parser.add_argument("--system-map", type=pathlib.Path)
    parser.add_argument("--build-root", type=pathlib.Path)
    parser.add_argument("--fpu-object-policy", type=pathlib.Path)
    parser.add_argument("--derived-fpu-inventory", type=pathlib.Path)
    parser.add_argument("--write-derived-fpu-inventory", type=pathlib.Path)
    parser.add_argument("--observations-input", type=pathlib.Path)
    parser.add_argument("--observations-output", type=pathlib.Path)
    args = parser.parse_args()

    _source_version, allowlist = load_allowlist(args.allowlist, args.lto_mode)
    derived_keys: set[tuple[str, str, str]] = set()
    derived_object_count = 0
    if (args.build_root is None) != (args.fpu_object_policy is None):
        raise SystemExit("--build-root and --fpu-object-policy must be supplied together")
    if args.build_root is not None and args.derived_fpu_inventory is not None:
        raise SystemExit("build-root derivation and a derived inventory are mutually exclusive")
    if args.write_derived_fpu_inventory is not None and args.build_root is None:
        raise SystemExit("--write-derived-fpu-inventory requires --build-root")
    if args.build_root is not None and args.fpu_object_policy is not None:
        derived, derived_object_count = derive_fpu_symbols(
            args.build_root, args.fpu_object_policy, args.llvm_major
        )
        if args.write_derived_fpu_inventory is not None:
            write_derived_fpu_inventory(
                args.write_derived_fpu_inventory,
                derived,
                derived_object_count,
                args.llvm_major,
            )
    elif args.derived_fpu_inventory is not None:
        derived, derived_object_count = load_derived_fpu_inventory(
            args.derived_fpu_inventory, args.llvm_major
        )
    else:
        derived = {}
    if derived:
        overlap = set(allowlist) & set(derived)
        if overlap:
            raise SystemExit(f"manual SIMD allowlist duplicates derived FPU symbols: {sorted(overlap)[:5]}")
        allowlist.update(derived)
        derived_keys = set(derived)
    tool = f"llvm-objdump-{args.llvm_major}"
    if args.observations_input is not None:
        if args.observations_output is not None:
            raise SystemExit("observation replay cannot also write a scan inventory")
        observed, module_count, total_instructions, total_matches = load_observations(
            args.observations_input, args.llvm_major
        )
    else:
        if shutil.which(tool) is None:
            raise SystemExit(f"selected disassembler is unavailable: {tool}")
        if not args.vmlinux.is_file():
            raise SystemExit(f"vmlinux is absent: {args.vmlinux}")
        observed: Counter[tuple[str, str, str]] = Counter()
        total_instructions = total_matches = 0
        module_count = 0
        with tempfile.TemporaryDirectory(prefix="dkc-simd-", dir="/work") as temp_name:
            temporary = pathlib.Path(temp_name)
            extracted = temporary / "packages"
            extracted.mkdir()
            for deb in sorted(args.artifacts.glob("*.deb")):
                subprocess.run(["dpkg-deb", "--extract", deb, extracted], check=True)

            instructions, matches = scan_binary(
                tool,
                args.vmlinux,
                "vmlinux",
                SymbolMap(args.system_map),
                observed,
            )
            total_instructions += instructions
            total_matches += matches

            modules = sorted(
                path
                for path in extracted.rglob("*.ko*")
                if path.name.endswith((".ko", ".ko.xz", ".ko.zst"))
            )
            if len(modules) < 1000:
                raise SystemExit(f"refusing incomplete module audit: only {len(modules)} modules")
            module_work = temporary / "module"
            module_work.mkdir()
            seen: set[str] = set()
            for module in modules:
                artifact = module_identity(module)
                if artifact in seen:
                    raise SystemExit(f"duplicate shipped module identity: {artifact}")
                seen.add(artifact)
                materialized = materialize_module(module, module_work)
                instructions, matches = scan_binary(
                    tool,
                    materialized,
                    artifact,
                    SymbolMap(None),
                    observed,
                )
                materialized.unlink()
                total_instructions += instructions
                total_matches += matches
                module_count += 1
        if args.observations_output is not None:
            write_json(
                args.observations_output,
                observation_document(
                    observed,
                    args.llvm_major,
                    module_count,
                    total_instructions,
                    total_matches,
                ),
            )

    used, unexpected, unused, aliases = evaluate_observations(
        observed, allowlist, derived_keys
    )
    unexpected_rows = [
        {
            "artifact": artifact,
            kind: selector,
            "instruction_count": count,
        }
        for (artifact, kind, selector), count in sorted(unexpected.items())
    ]
    report = {
        "schema_version": 3,
        "status": "PASS" if not unexpected and not unused else "FAIL",
        "lto_mode": args.lto_mode,
        "llvm_objdump": tool,
        "vmlinux_files": 1,
        "module_files": module_count,
        "decoded_instructions": total_instructions,
        "simd_fpu_instructions": total_matches,
        "allowlist_entries": len(allowlist),
        "derived_fpu_objects": derived_object_count,
        "derived_fpu_symbols": len(derived_keys),
        "used_allowlist_entries": len(used),
        "lto_symbol_aliases": aliases,
        "unexpected": unexpected_rows,
        "unused_allowlist": [
            {"artifact": artifact, kind: selector}
            for artifact, kind, selector in unused
        ],
    }
    write_json(args.report, report)
    if unexpected or unused:
        raise SystemExit(
            "SIMD audit FAIL: "
            f"{len(unexpected)} unreviewed exact symbol(s), {len(unused)} stale allowlist entry(s)"
        )
    print(
        f"SIMD audit PASS: vmlinux + {module_count} modules, "
        f"{total_matches} instructions confined to {len(used)} reviewed entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
