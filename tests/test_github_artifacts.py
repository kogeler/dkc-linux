from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from dkc.evidence import verify_evidence_directory
from dkc.github_artifacts import (
    prepare_flavor_evidence,
    prepare_pull_request_repository_evidence,
)
from dkc.release_cache import prepare_release_cache, release_cache_identity
from tests.test_release_cache import (
    IMAGE,
    ROOT,
    TOOLBOX,
    _accepted_results,
    _decision,
)


def _write(path: Path, body: str = "value\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _flavor_cache(root: Path) -> Path:
    root.mkdir()
    decision_root = root / "decision"
    decision = _decision(decision_root, "qualification")
    flavor, selftest, qemu = _accepted_results(root / "results", decision)
    _write(flavor / "evidence/capacity.env")
    _write(qemu / "evidence/package-audit.json", "{}\n")
    _write(qemu / "evidence/evidence.sha256", "full result manifest\n")
    _write(qemu / "v3/evidence/serial.log.xz")
    _write(qemu / "v3/evidence/evidence.sha256", "full scenario manifest\n")
    _write(qemu / "v3/guest/kselftest-summary.env")
    _write(qemu / "v3/guest/kselftest-skips.log")

    workspace = root / "workspace"
    workspace.mkdir()
    cache = workspace / "out/release-cache/v3"
    identity = release_cache_identity(decision, flavor="v3", repository_root=ROOT)
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
    return cache


def test_flavor_artifact_is_bounded_exact_and_idempotent(tmp_path: Path) -> None:
    cache = _flavor_cache(tmp_path / "cache")
    output = tmp_path / "output"
    assert (cache / "kselftest/evidence/result.env").is_file()
    assert not (cache / "selftest").exists()

    prepare_flavor_evidence(cache, output, flavor="v3")
    paths = verify_evidence_directory(output)
    assert "selftest/evidence/result.env" in paths
    assert "qemu/v3/guest/kselftest-summary.env" in paths
    assert "flavor/evidence/kernel-simd-audit.json" in paths
    assert not any(path.endswith(".deb") for path in paths)
    assert not any(path.endswith("/evidence.sha256") for path in paths)
    assert json.loads((output / "bundle.json").read_text())["flavor"] == "v3"

    initial = (output / "evidence.sha256").read_bytes()
    prepare_flavor_evidence(cache, output, flavor="v3")
    assert (output / "evidence.sha256").read_bytes() == initial

    _write(cache / "qemu/v3/guest/kselftest-summary.env", "changed\n")
    with pytest.raises(ValueError, match="existing artifact evidence differs"):
        prepare_flavor_evidence(cache, output, flavor="v3")


def _apt_results(root: Path) -> tuple[Path, Path, Path]:
    unsigned = root / "unsigned"
    signature = root / "signature"
    repository = root / "repository"
    _write(
        unsigned / "evidence/result.env",
        "status=PASS\nrepository_assembly=PASS\npublishable=false\n",
    )
    _write(unsigned / "evidence/assembly.json", "{}\n")
    _write(
        signature / "evidence/result.env",
        "status=PASS\nrepository_signing=PASS\npublishable=false\n",
    )
    _write(signature / "evidence/signing.json", "{}\n")
    _write(
        repository / "evidence/result.env",
        "status=PASS\nsigned_apt_client=PASS\npublishable=false\n",
    )
    _write(repository / "evidence/merge.json", "{}\n")
    _write(repository / "client/result.env", "status=PASS\napt_secure=PASS\n")
    _write(repository / "client/source-rebuild.json", "{}\n")
    _write(repository / "client/download/archive-keyring.deb", "package bytes\n")
    for result in (unsigned, signature, repository):
        _write(
            result / "evidence/evidence.sha256",
            "0" * 64 + "  ./repository/intentionally-not-retained.deb\n",
        )
    return unsigned, signature, repository


def test_pull_request_repository_artifact_has_its_own_exact_boundary(
    tmp_path: Path,
) -> None:
    unsigned, signature, repository = _apt_results(tmp_path / "results")
    output = tmp_path / "artifact"
    prepare_pull_request_repository_evidence(
        output,
        unsigned_result=unsigned,
        signature_result=signature,
        repository_result=repository,
        qualification_outcome="success",
    )

    paths = verify_evidence_directory(output)
    assert "unsigned/evidence/assembly.json" in paths
    assert "signature/evidence/signing.json" in paths
    assert "repository/client/source-rebuild.json" in paths
    assert not any(path.endswith(".deb") for path in paths)
    assert not any(path.endswith("/evidence.sha256") for path in paths)
    assert "intentionally-not-retained" not in (output / "evidence.sha256").read_text()
    metadata = json.loads((output / "bundle.json").read_text())
    assert metadata["complete"] is True
    assert metadata["qualification_outcome"] == "success"


def test_repository_evidence_requires_complete_success_but_keeps_failures(
    tmp_path: Path,
) -> None:
    unsigned, signature, repository = _apt_results(tmp_path / "results")
    shutil.rmtree(repository / "client")

    with pytest.raises(ValueError, match="lacks bounded evidence"):
        prepare_pull_request_repository_evidence(
            tmp_path / "success",
            unsigned_result=unsigned,
            signature_result=signature,
            repository_result=repository,
            qualification_outcome="success",
        )

    output = tmp_path / "failure"
    prepare_pull_request_repository_evidence(
        output,
        unsigned_result=unsigned,
        signature_result=signature,
        repository_result=repository,
        qualification_outcome="failure",
    )
    verify_evidence_directory(output)
    assert json.loads((output / "bundle.json").read_text())["complete"] is False


def test_artifact_evidence_rejects_links(tmp_path: Path) -> None:
    cache = _flavor_cache(tmp_path / "cache")
    target = cache / "qemu/v3/guest/kselftest-summary.env"
    target.unlink()
    target.symlink_to(cache / "qemu/v3/guest/result.env")
    with pytest.raises(ValueError, match="symbolic link"):
        prepare_flavor_evidence(cache, tmp_path / "output", flavor="v3")


def test_artifact_evidence_rejects_oversized_reports(tmp_path: Path) -> None:
    cache = _flavor_cache(tmp_path / "cache")
    with (cache / "qemu/v3/evidence/serial.log.xz").open("wb") as stream:
        stream.truncate(8 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="file exceeds its size limit"):
        prepare_flavor_evidence(cache, tmp_path / "output", flavor="v3")


def test_flavor_artifact_rejects_an_invalid_payload_map(tmp_path: Path) -> None:
    cache = _flavor_cache(tmp_path / "cache")
    manifest = json.loads((cache / "cache.json").read_text())
    manifest["payload"]["kselftest"] = manifest["payload"]["flavor"]
    (cache / "cache.json").write_text(json.dumps(manifest) + "\n")
    with pytest.raises(ValueError, match="invalid payload path"):
        prepare_flavor_evidence(cache, tmp_path / "output", flavor="v3")
