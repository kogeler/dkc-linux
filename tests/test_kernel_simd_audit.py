"""Policy fixtures for the full final-ELF SIMD/FPU audit."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from collections import Counter

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "in-container" / "audit-kernel-simd.py"
spec = importlib.util.spec_from_file_location("audit_kernel_simd", SCRIPT)
assert spec and spec.loader
audit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = audit
spec.loader.exec_module(audit)


def test_audit_has_no_non_fatal_discovery_mode() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "--discover" not in source
    assert '"status": "DISCOVERY"' not in source


def test_manual_allowlist_is_exact_sorted_and_source_versioned() -> None:
    version, entries = audit.load_allowlist(
        ROOT / "config/flavors/intentional-simd-symbols.toml"
    )
    assert version == "7.1.7-1"
    assert len(entries) == 233
    assert list(entries) == sorted(entries)
    assert all("*" not in part and "?" not in part for key in entries for part in key)
    assert (
        "kernel/arch/x86/kvm/kvm.ko",
        "symbol",
        "fetch_possible_mmx_operand",
    ) in entries
    for symbol in ("em_fnstcw", "em_fnstsw", "flush_pending_x87_faults"):
        assert ("kernel/arch/x86/kvm/kvm.ko", "symbol", symbol) in entries
    assert ("vmlinux", "symbol", "fpu__drop") in entries
    assert ("vmlinux", "symbol", "restore_fpregs_from_fpstate") in entries

    _version, thin_entries = audit.load_allowlist(
        ROOT / "config/flavors/intentional-simd-symbols.toml", "thin"
    )
    _version, full_entries = audit.load_allowlist(
        ROOT / "config/flavors/intentional-simd-symbols.toml", "full"
    )
    assert len(thin_entries) == 227
    assert len(full_entries) == 233
    assert (
        "kernel/drivers/gpu/drm/i915/i915.ko",
        "symbol",
        "intel_guc_log_dump",
    ) not in thin_entries
    assert (
        "kernel/drivers/gpu/drm/i915/i915.ko",
        "symbol",
        "intel_guc_log_dump",
    ) in full_entries


def test_allowlist_rejects_invalid_lto_mode_sets(tmp_path: pathlib.Path) -> None:
    for modes in ('["thin", "none"]', '[]', '["future"]', '["full", "full"]'):
        policy = tmp_path / "policy.toml"
        policy.write_text(
            f'''schema_version = 1
source_version = "7.1.7-1"

[[entry]]
artifact = "vmlinux"
symbol = "reviewed"
reason = "fixture"
lto_modes = {modes}
''',
            encoding="utf-8",
        )
        with pytest.raises(SystemExit, match="invalid SIMD allowlist LTO modes"):
            audit.load_allowlist(policy, "thin")


def test_register_matcher_covers_all_forbidden_vector_state() -> None:
    for register in (
        "%mm0",
        "%xmm31",
        "%ymm4",
        "%zmm15",
        "%k7",
        "%tmm6",
        "%bnd3",
        "%st",
        "%st(6)",
    ):
        assert audit.REGISTER.search(register), register
    assert not audit.REGISTER.search("%rax")


def test_implicit_fpu_and_extended_state_instructions_are_detected() -> None:
    for mnemonic in (
        "fldz",
        "fninit",
        "fnstcw",
        "fnstenv",
        "fnsave",
        "fwait",
        "wait",
        "fxsave64",
        "xrstors64",
    ):
        assert (
            mnemonic in audit.IMPLICIT
            or audit.X87.match(mnemonic)
            or audit.EXTENDED_STATE.fullmatch(mnemonic)
        ), mnemonic
    for mnemonic in ("ldmxcsr", "ldtilecfg", "tilerelease", "vzeroupper"):
        assert mnemonic in audit.IMPLICIT
    assert audit.X87.match("fence") is None
    assert audit.EXTENDED_STATE.fullmatch("xsaveopt64")


def test_fpu_symbols_are_derived_from_exact_reviewed_objects(
    tmp_path: pathlib.Path, monkeypatch: object
) -> None:
    import tomllib

    policy = ROOT / "config/flavors/intentional-fpu-objects.toml"
    objects = tomllib.loads(policy.read_text(encoding="utf-8"))["objects"]
    for relative in objects:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def fake_nm(command: list[object], **_kwargs: object) -> str:
        stem = pathlib.Path(command[-1]).stem.replace("-", "_")
        return "".join(f"{stem}_exact_{index} T {index:x} 1\n" for index in range(10))

    monkeypatch.setattr(audit.subprocess, "check_output", fake_nm)  # type: ignore[attr-defined]
    entries, object_count = audit.derive_fpu_symbols(tmp_path, policy, 21)
    assert object_count == 66
    assert len(entries) >= 500
    assert {
        entry.artifact for entry in entries.values()
    } == {"kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko"}


def test_lto_internal_suffix_resolves_only_to_an_existing_exact_symbol() -> None:
    entry = audit.AllowEntry("vmlinux", "symbol", "fpregs_restore_userregs", "reviewed")
    allowlist = {entry.key: entry}
    assert audit.reviewed_key(
        ("vmlinux", "symbol", "fpregs_restore_userregs.llvm.16097277919764810943"),
        allowlist,
    ) == entry.key
    for key in (
        ("vmlinux", "symbol", "unreviewed.llvm.1"),
        ("vmlinux", "symbol", "fpregs_restore_userregs.llvm.not-a-number"),
        ("other", "symbol", "fpregs_restore_userregs.llvm.1"),
        ("vmlinux", "section", "fpregs_restore_userregs.llvm.1"),
    ):
        assert audit.reviewed_key(key, allowlist) is None


def test_full_lto_numeric_suffix_is_limited_to_derived_fpu_symbols() -> None:
    derived = audit.AllowEntry(
        "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
        "symbol",
        "CalculateFlipSchedule",
        "derived from a reviewed object",
    )
    manual = audit.AllowEntry("vmlinux", "symbol", "xsaves", "reviewed")
    allowlist = {derived.key: derived, manual.key: manual}
    assert audit.reviewed_key(
        (
            "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
            "symbol",
            "CalculateFlipSchedule.21322",
        ),
        allowlist,
        {derived.key},
    ) == derived.key
    for key in (
        ("vmlinux", "symbol", "xsaves.42"),
        (
            "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
            "symbol",
            "Unreviewed.42",
        ),
        (
            "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
            "symbol",
            "CalculateFlipSchedule.not-a-number",
        ),
        ("kernel/other.ko", "symbol", "CalculateFlipSchedule.42"),
        (
            "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
            "section",
            "CalculateFlipSchedule.42",
        ),
    ):
        assert audit.reviewed_key(key, allowlist, {derived.key}) is None


def test_full_lto_observation_replay_records_numeric_aliases() -> None:
    entry = audit.AllowEntry(
        "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
        "symbol",
        "CalculateFlipSchedule",
        "derived from a reviewed object",
    )
    observed = Counter(
        {
            (
                "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
                "symbol",
                "CalculateFlipSchedule.21322",
            ): 425
        }
    )
    used, unexpected, unused, aliases = audit.evaluate_observations(
        observed, {entry.key: entry}, {entry.key}
    )
    assert used == Counter({entry.key: 425})
    assert unexpected == Counter()
    assert unused == []
    assert aliases == [
        {
            "artifact": "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
            "observed_symbol": "CalculateFlipSchedule.21322",
            "reviewed_symbol": "CalculateFlipSchedule",
        }
    ]


def test_thin_lto_symbol_aliases_resolve_to_the_existing_reviewed_policy() -> None:
    _version, allowlist = audit.load_allowlist(
        ROOT / "config/flavors/intentional-simd-symbols.toml"
    )
    for symbol in ("dcn_bw_pow", "dml_core_mode_programming"):
        entry = audit.AllowEntry(
            "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
            "symbol",
            symbol,
            "defined by a reviewed CC_FLAGS_FPU object",
        )
        allowlist[entry.key] = entry
    observed = (
        ("kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko", "dcn_bw_pow.llvm.283786859739323551"),
        ("kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko", "dml_core_mode_programming.llvm.8275309366420712261"),
        ("kernel/lib/raid6/raid6_pq.ko", "raid6_2data_recov_avx2.llvm.10557344844962565219"),
        ("kernel/lib/raid6/raid6_pq.ko", "raid6_2data_recov_avx512.llvm.4684959856459538586"),
        ("kernel/lib/raid6/raid6_pq.ko", "raid6_2data_recov_ssse3.llvm.13827144924319529485"),
        ("kernel/lib/raid6/raid6_pq.ko", "raid6_avx5124_gen_syndrome.llvm.15708548373724094443"),
        ("kernel/lib/raid6/raid6_pq.ko", "raid6_avx5124_xor_syndrome.llvm.15708548373724094443"),
        ("kernel/lib/raid6/raid6_pq.ko", "raid6_datap_recov_avx2.llvm.10557344844962565219"),
        ("kernel/lib/raid6/raid6_pq.ko", "raid6_datap_recov_avx512.llvm.4684959856459538586"),
        ("kernel/lib/raid6/raid6_pq.ko", "raid6_datap_recov_ssse3.llvm.13827144924319529485"),
        ("vmlinux", "fpregs_restore_userregs.llvm.16097277919764810943"),
    )
    for artifact, symbol in observed:
        assert audit.reviewed_key((artifact, "symbol", symbol), allowlist) is not None


def test_observation_replay_preserves_counts_and_lto_aliases(
    tmp_path: pathlib.Path,
) -> None:
    observed = Counter(
        {
            ("vmlinux", "symbol", "fpregs_restore_userregs.llvm.123"): 5,
            ("vmlinux", "symbol", "unreviewed"): 2,
        }
    )
    path = tmp_path / "observations.json.xz"
    audit.write_json(path, audit.observation_document(observed, 21, 4225, 100, 7))
    loaded, modules, instructions, matches = audit.load_observations(path, 21)
    assert loaded == observed
    assert (modules, instructions, matches) == (4225, 100, 7)

    entry = audit.AllowEntry("vmlinux", "symbol", "fpregs_restore_userregs", "reviewed")
    used, unexpected, unused, aliases = audit.evaluate_observations(
        loaded, {entry.key: entry}, set()
    )
    assert used == Counter({entry.key: 5})
    assert unexpected == Counter({("vmlinux", "symbol", "unreviewed"): 2})
    assert unused == []
    assert aliases == [
        {
            "artifact": "vmlinux",
            "observed_symbol": "fpregs_restore_userregs.llvm.123",
            "reviewed_symbol": "fpregs_restore_userregs",
        }
    ]


def test_derived_fpu_inventory_round_trips_without_build_objects(
    tmp_path: pathlib.Path,
) -> None:
    entries = {}
    for index in range(500):
        entry = audit.AllowEntry(
            "kernel/drivers/gpu/drm/amd/amdgpu/amdgpu.ko",
            "symbol",
            f"derived_{index:03d}",
            "reviewed object",
        )
        entries[entry.key] = entry
    path = tmp_path / "derived.json"
    audit.write_derived_fpu_inventory(path, entries, 66, 21)
    loaded, object_count = audit.load_derived_fpu_inventory(path, 21)
    assert loaded == entries
    assert object_count == 66
