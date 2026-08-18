from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from dkc.buildpolicy import build_policy_digest
from dkc.records import DiscoveryDecision
from dkc.release_cache import (
    prepare_release_cache,
    release_cache_identity,
    verify_release_cache,
)
from dkc.release_gate import discovery_decision_outputs
from dkc.serialize import dumps


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 64
IMAGE = "ghcr.io/kogeler/dkc-kernel-build@sha256:" + "d" * 64
TOOLBOX = "ghcr.io/kogeler/dkc-toolbox@sha256:" + "c" * 64


def _inventory(root: Path) -> None:
    records = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "evidence.sha256":
            name = path.relative_to(root).as_posix()
            records.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {name}")
    (root / "evidence.sha256").write_text("\n".join(records) + "\n")


def _decision(root: Path) -> DiscoveryDecision:
    value = DiscoveryDecision(
        decision="build",
        source_version="7.1.7-1",
        source_dsc_sha256=SHA,
        dkc_revision=1,
        build_policy_sha256=build_policy_digest(ROOT),
        lto_mode="thin",
        utc="2026-08-17T12:00:00Z",
        build_required=True,
        publish_allowed=True,
        authoritative_state_read=True,
    )
    root.mkdir()
    (root / "decision.json").write_text(dumps(value.to_dict()))
    (root / "outputs.env").write_text(
        "".join(
            f"{key}={field}\n"
            for key, field in sorted(discovery_decision_outputs(value).items())
        )
    )
    (root / "result.env").write_text("status=PASS\nlifecycle_decision=build\n")
    _inventory(root)
    return value


def _accepted_results(root: Path, decision: DiscoveryDecision) -> tuple[Path, Path, Path]:
    flavor = root / "flavor"
    selftest = root / "selftest"
    qemu = root / "qemu"
    for path in (
        flavor / "evidence",
        flavor / "artifacts",
        selftest / "evidence",
        qemu / "evidence",
        qemu / "v3/evidence",
        qemu / "v3/guest",
    ):
        path.mkdir(parents=True)
    package = flavor / "artifacts/dkc-linux-test.deb"
    package.write_bytes(b"accepted package\n")
    package_sha = hashlib.sha256(package.read_bytes()).hexdigest()
    kernel_release = "7.1.7+dkc13.r1.g0123456789ab-v3-amd64"
    build_digest = "b" * 64
    (flavor / "evidence/result.env").write_text(
        "status=PASS\nflavor=v3\nlto_mode=thin\n"
    )
    (flavor / "evidence/publication-identity.json").write_text(
        dumps(
            {
                "build_input_digest": build_digest,
                "build_inputs": {
                    "debian_source": {
                        "version": decision.source_version,
                        "dsc_sha256": decision.source_dsc_sha256,
                    },
                    "dkc_revision": decision.dkc_revision,
                    "overlay_sha256": decision.build_policy_sha256,
                    "lto_mode": decision.lto_mode,
                },
                "kernel_releases": {"v3": kernel_release},
            }
        )
    )
    (flavor / "evidence/build-image-provenance.env").write_text(
        "provider=registry\nregistry_manifest_digest=sha256:" + "d" * 64 + "\n"
    )
    (flavor / "evidence/post-build-gates.env").write_text(
        "package_attestation_rc=0\n"
        "kbuild_audit_rc=0\n"
        "simd_audit_rc=0\n"
        "lintian_rc=0\n"
    )
    for name in ("kernel-simd-audit.json", "kbuild-command-audit.json"):
        (flavor / "evidence" / name).write_text(
            dumps({"status": "PASS", "lto_mode": "thin"})
        )
    (flavor / "evidence/attestation.json").write_text(
        dumps(
            {
                "status": "PASS",
                "flavor": "v3",
                "kernel_release": kernel_release,
                "lto_mode": "thin",
                "packages": {package.name: package_sha},
            }
        )
    )
    (selftest / "evidence/result.env").write_text(
        "status=PASS\nflavor=v3\nprofile_kind=qualification\n"
        f"kernel_release={kernel_release}\nlto_mode=thin\n"
    )
    (selftest / "evidence/kselftest-build.json").write_text(
        dumps(
            {
                "status": "PASS",
                "flavor": "v3",
                "profile_kind": "qualification",
                "kernel_release": kernel_release,
                "lto_mode": "thin",
                "build_input_digest": build_digest,
            }
        )
    )
    (qemu / "evidence/result.env").write_text(
        "status=PASS\nflavor=v3\nlto_mode=thin\naccelerator=kvm\n"
    )
    (qemu / "v3/evidence/result.json").write_text(
        dumps(
            {
                "status": "PASS",
                "flavor": "v3",
                "accelerator": "kvm",
                "qemu_exit": 0,
            }
        )
    )
    (qemu / "v3/guest/result.env").write_text(
        "status=PASS\nflavor=v3\n"
        f"target_kernel={kernel_release}\nqualification=PASS\n"
    )
    return flavor, selftest, qemu


def test_release_cache_key_binds_source_policy_and_flavor_not_image_rollover() -> None:
    decision = DiscoveryDecision(
        decision="build",
        source_version="7.1.7-1",
        source_dsc_sha256=SHA,
        dkc_revision=1,
        build_policy_sha256="b" * 64,
        lto_mode="thin",
        utc="2026-08-17T12:00:00Z",
        build_required=True,
        publish_allowed=True,
        authoritative_state_read=True,
    )
    first = release_cache_identity(decision, flavor="v2", repository_root=ROOT)
    second = release_cache_identity(decision, flavor="v3", repository_root=ROOT)
    assert first.key() != second.key()
    assert len(first.key()) < 512
    changed = DiscoveryDecision(
        **{**decision.__dict__, "build_policy_sha256": "c" * 64}
    )
    assert (
        release_cache_identity(changed, flavor="v2", repository_root=ROOT).key()
        != first.key()
    )


def test_release_cache_is_sealed_verified_idempotent_and_tamper_evident(
    tmp_path: Path,
) -> None:
    decision_root = tmp_path / "decision"
    decision = _decision(decision_root)
    flavor, selftest, qemu = _accepted_results(tmp_path / "results", decision)
    identity = release_cache_identity(decision, flavor="v3", repository_root=ROOT)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cache = workspace / "out/release-cache/v3"
    prepare_release_cache(
        cache,
        flavor_result=flavor,
        selftest_result=selftest,
        qemu_result=qemu,
        decision_root=decision_root,
        flavor="v3",
        build_image=IMAGE,
        toolbox_image=TOOLBOX,
        expected_key=identity.key(),
        repository_root=ROOT,
        cache_workspace=workspace,
    )
    manifest = json.loads((cache / "cache.json").read_text())
    assert set(manifest["identity"]) == {
        "schema_version",
        "flavor",
        "source_version",
        "source_dsc_sha256",
        "dkc_revision",
        "build_policy_sha256",
        "validation_policy_sha256",
        "lto_mode",
    }
    assert manifest["provenance"] == {
        "build_image": IMAGE,
        "toolbox_image": TOOLBOX,
    }
    initial = (cache / "evidence.sha256").read_bytes()
    prepare_release_cache(
        cache,
        flavor_result=flavor,
        selftest_result=selftest,
        qemu_result=qemu,
        decision_root=decision_root,
        flavor="v3",
        build_image=IMAGE,
        toolbox_image=TOOLBOX,
        expected_key=identity.key(),
        repository_root=ROOT,
        cache_workspace=workspace,
    )
    assert (cache / "evidence.sha256").read_bytes() == initial
    verify_release_cache(
        cache,
        decision_root=decision_root,
        flavor="v3",
        expected_key=identity.key(),
        repository_root=ROOT,
        cache_workspace=workspace,
    )
    restored_workspace = tmp_path / "restored-workspace"
    restored_workspace.mkdir()
    restored_cache = restored_workspace / "out/release-cache/v3"
    shutil.copytree(cache, restored_cache)
    verify_release_cache(
        restored_cache,
        decision_root=decision_root,
        flavor="v3",
        expected_key=identity.key(),
        repository_root=ROOT,
        cache_workspace=restored_workspace,
    )
    (cache / "flavor/artifacts/dkc-linux-test.deb").write_bytes(b"tampered\n")
    with pytest.raises(ValueError, match="checksum verification"):
        verify_release_cache(
            cache,
            decision_root=decision_root,
            flavor="v3",
            expected_key=identity.key(),
            repository_root=ROOT,
            cache_workspace=workspace,
        )


def test_release_cache_location_is_confined(tmp_path: Path) -> None:
    decision_root = tmp_path / "decision"
    decision = _decision(decision_root)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    key = release_cache_identity(decision, flavor="v3", repository_root=ROOT).key()
    with pytest.raises(ValueError, match="outside"):
        verify_release_cache(
            tmp_path / "elsewhere",
            decision_root=decision_root,
            flavor="v3",
            expected_key=key,
            repository_root=ROOT,
            cache_workspace=workspace,
        )

    external = tmp_path / "external"
    external.mkdir()
    (workspace / "out").symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        verify_release_cache(
            workspace / "out/release-cache/v3",
            decision_root=decision_root,
            flavor="v3",
            expected_key=key,
            repository_root=ROOT,
            cache_workspace=workspace,
        )
    assert external.is_dir()
