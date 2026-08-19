"""Safety checks for the host-side shell boundary."""

from __future__ import annotations

import os
import pathlib
import re
import shlex
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _source_common(**environment: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(environment)
    return subprocess.run(
        ["bash", "-c", "source scripts/lib/common.sh"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _normalize_podman_image_id(value: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            "-c",
            'source scripts/lib/podman-image.sh; dkc::canonical_podman_image_id "$1"',
            "bash",
            value,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_run_id_cannot_escape_the_scratch_directory() -> None:
    result = _source_common(DKC_RUN_ID="../../outside")
    assert result.returncode != 0
    assert "unsafe DKC_RUN_ID" in result.stderr


def test_environment_cannot_redirect_the_repository_root() -> None:
    result = _source_common(DKC_ROOT="/tmp")
    assert result.returncode != 0
    assert "does not identify this script repository" in result.stderr


def test_run_scratch_rejects_a_preexisting_symlink(tmp_path: pathlib.Path) -> None:
    run_id = "20260817T140000Z-deadbeef"
    run_root = ROOT / ".dkc-run"
    run_root.mkdir(exist_ok=True)
    run_path = run_root / run_id
    target = tmp_path / "outside"
    target.mkdir()
    run_path.symlink_to(target, target_is_directory=True)
    try:
        result = subprocess.run(
            [
                "bash",
                "-c",
                "source scripts/lib/common.sh; dkc::install_cleanup_trap",
            ],
            cwd=ROOT,
            env={**os.environ, "DKC_RUN_ID": run_id},
            text=True,
            capture_output=True,
            check=False,
        )
    finally:
        run_path.unlink(missing_ok=True)
    assert result.returncode != 0
    assert "run scratch path must be a real directory" in result.stderr
    assert not (target / "resources.tsv").exists()


def test_podman_config_id_is_normalized_to_an_algorithm_digest() -> None:
    bare = "12" * 32
    canonical = f"sha256:{bare}"
    for value in (bare, canonical):
        result = _normalize_podman_image_id(value)
        assert result.returncode == 0, result.stderr
        assert result.stdout == f"{canonical}\n"
    for value in ("", "sha256:1234", "SHA256:" + bare, "gg" * 32):
        assert _normalize_podman_image_id(value).returncode != 0


def test_ci_uses_only_standard_runners_and_has_no_temporary_capacity_job() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow = workflow_path.read_text()
    parsed = yaml.safe_load(workflow)
    events = parsed.get("on", parsed.get(True))
    jobs = parsed["jobs"]
    assert workflow.count("runs-on: ubuntu-26.04") == len(jobs)
    assert "runs-on: ${{" not in workflow
    assert events["workflow_dispatch"]["inputs"]["confirm_lifecycle"]["type"] == "boolean"
    assert events["workflow_dispatch"]["inputs"]["allow_empty_bootstrap"]["type"] == "boolean"
    assert "push" not in events
    assert events["pull_request"] == {"branches": ["main"]}
    assert events["schedule"] == [{"cron": "17 */6 * * *"}]
    assert jobs["lifecycle-gate"]["if"] == "github.event_name != 'pull_request'"
    assert jobs["container_images"]["if"] == (
        "github.event_name == 'pull_request' || "
        "(github.repository == 'kogeler/dkc-linux' && "
        "github.ref == 'refs/heads/main')"
    )
    gate = jobs["lifecycle-gate"]["steps"][-1]
    assert gate["run"] == "make github-lifecycle-gate"
    flavor_condition = str(jobs["flavors"]["if"])
    assert flavor_condition.startswith("always() &&")
    assert "needs.lifecycle-decision.outputs.build_required == 'true'" in (
        flavor_condition
    )
    assert jobs["publish-repository"]["needs"][-1] == "verify-repository"
    assert jobs["publish-repository"]["if"] == (
        "always() && github.event_name != 'pull_request' && "
        "needs.verify-repository.result == 'success'"
    )
    assert jobs["verify-published-state"]["if"] == (
        "always() && github.event_name != 'pull_request' && "
        "needs.publish-repository.result == 'success'"
    )
    assert jobs["lifecycle-result"]["if"] == (
        "always() && github.event_name != 'pull_request' && "
        "github.repository == 'kogeler/dkc-linux' && "
        "github.ref == 'refs/heads/main' && "
        "needs.fast.result == 'success'"
    )
    assert "runner-cleanup" not in workflow
    assert "runner-inventory" not in workflow
    assert "sudo " not in workflow
    assert "apt-get" not in workflow
    assert "qemu-system" not in workflow
    assert "jq " not in workflow
    assert "grep " not in workflow
    assert "scripts/" not in workflow
    assert "udevadm" not in workflow
    assert "/etc/udev" not in workflow
    assert "flavor: [v2, v3]" in workflow
    assert "dkc-flavor-v4-" not in workflow
    assert "make build-flavor FLAVOR=" in workflow
    assert "make kselftest-flavor" in workflow
    assert "make qemu-boot-flavor" in workflow
    assert "KSELFTEST_SOURCE_RESULT" not in workflow
    assert "make github-kvm-prepare" in workflow
    assert "QEMU_ACCEL=kvm" in workflow
    assert "QEMU_ACCEL=auto" not in workflow
    assert "try TCG" not in workflow
    assert "use TCG" not in workflow
    assert "make package-matrix" in workflow
    assert "make apt-repository-assemble-lifecycle" in workflow
    assert "make github-apt-repository-sign" in workflow
    assert "make apt-repository-verify-decision" in workflow
    assert "make github-storage-state-read" in workflow
    assert "make github-storage-export-pool" in workflow
    assert "make github-storage-publish" in workflow
    assert "make storage-disposable" not in workflow
    assert workflow.count("name: production-signing") == 1
    assert workflow.count("APT_GPG_SIGNING_SUBKEY_B64: ${{ secrets.") == 1
    assert workflow.count("APT_GPG_PASSPHRASE: ${{ secrets.") == 1
    assert "make current-main" in workflow
    assert "actions/download-artifact@70fc10c6e5e1ce46ad2ea6f2b72d43f7d47b13c3" in workflow


def test_every_ci_command_is_one_declarative_make_invocation() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    forbidden = re.compile(
        r"(?:^|\s)(?:apt-get|awk|case|curl|grep|if|jq|podman|printf|python3|"
        r"qemu-system-x86_64|sed|sudo|test)(?:\s|$)|(?:&&|\|\||[;`<>])"
    )
    for job_name, job in workflow["jobs"].items():
        for step in job["steps"]:
            if "run" not in step:
                continue
            command = step["run"]
            assert not forbidden.search(command), (job_name, step["name"], command)
            words = shlex.split(command)
            assert len(words) >= 2, (job_name, step["name"], command)
            assert words[0] == "make", (job_name, step["name"], command)
            assert re.fullmatch(r"[a-z][a-z0-9-]*", words[1]), (
                job_name,
                step["name"],
                command,
            )
            for assignment in words[2:]:
                assert re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", assignment, re.DOTALL), (
                    job_name,
                    step["name"],
                    assignment,
                )


def test_github_only_implementation_has_prefixed_make_targets() -> None:
    makefile = (ROOT / "mk" / "github.mk").read_text()
    targets = re.findall(r"^([a-z][a-z0-9-]*):", makefile, re.MULTILINE)
    assert targets
    assert all(target.startswith("github-") for target in targets)
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for script in (ROOT / "scripts").glob("github-*"):
        assert f"scripts/{script.name}" not in workflow


def test_non_image_ci_gates_do_not_evaluate_the_image_fingerprint() -> None:
    result = subprocess.run(
        [
            "make",
            "-n",
            "github-lifecycle-result",
            "DKC_IMAGE_HELPER=/path/that/does/not/exist",
            "GITHUB_LIFECYCLE_DECISION=no_op",
            "GITHUB_LIFECYCLE_DECISION_RESULT=success",
            "GITHUB_FINAL_STATE_RESULT=skipped",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "fingerprint" not in result.stderr


def test_ci_uploads_only_outputs_whose_producer_step_started() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    jobs = workflow["jobs"]
    expected = {
        "package-matrix": {
            "Upload package lifecycle evidence": "package_matrix",
            "Upload unsigned repository handoff": "assemble_repository",
        },
        "refresh-metadata": {
            "Upload unsigned repository handoff": "assemble_repository",
        },
        "sign-repository": {
            "Upload the bounded signature overlay": "sign_repository",
        },
        "verify-repository": {
            "Upload the complete verified repository": "verify_repository",
        },
        "publish-repository": {
            "Upload sanitized publication evidence": "publish",
        },
    }
    for job_name, uploads in expected.items():
        steps = {step["name"]: step for step in jobs[job_name]["steps"]}
        for upload_name, producer_id in uploads.items():
            assert steps[upload_name]["if"] == (
                f"${{{{ always() && steps.{producer_id}.outcome != 'skipped' }}}}"
            )
            assert any(step.get("id") == producer_id for step in steps.values())
    flavor_steps = jobs["flavors"]["steps"]
    assert not any(
        str(step.get("uses", "")).startswith("actions/upload-artifact@")
        for step in flavor_steps
    )


def test_ci_verifies_the_exact_package_lifecycle_artifact_before_upload() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    steps = workflow["jobs"]["package-matrix"]["steps"]
    verifier = next(
        step
        for step in steps
        if step["name"] == "Verify the package lifecycle artifact boundary"
    )
    uploader = next(
        step for step in steps if step["name"] == "Upload package lifecycle evidence"
    )
    assert verifier["run"].startswith("make package-matrix-verify-lifecycle ")
    assert "package-matrix-manifest.sh verify-lifecycle" in (
        ROOT / "mk" / "build.mk"
    ).read_text()
    assert verifier["if"] == (
        "${{ always() && steps.package_matrix.outcome != 'skipped' }}"
    )
    assert "flat-repository" not in uploader["with"]["path"]


def test_package_matrix_checksum_scopes_are_independently_verifiable(
    tmp_path: pathlib.Path,
) -> None:
    root = tmp_path / "full"
    for relative in ("evidence", "client-image", "client-headers", "flat-repository"):
        (root / relative).mkdir(parents=True)
    files = {
        "evidence/result.env": b"status=PASS\n",
        "client-image/result.env": b"status=PASS\n",
        "client-headers/result.env": b"status=PASS\n",
        "flat-repository/package.deb": b"package bytes\n",
    }
    for relative, content in files.items():
        (root / relative).write_bytes(content)

    verifier = ROOT / "scripts" / "package-matrix-manifest.sh"
    result = subprocess.run(
        [verifier, "write", root], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    lifecycle_manifest = (root / "evidence" / "evidence.sha256").read_text()
    assert "evidence/flat-repository.sha256" in lifecycle_manifest
    assert "flat-repository/package.deb" not in lifecycle_manifest
    assert "flat-repository/package.deb" in (
        root / "evidence" / "flat-repository.sha256"
    ).read_text()
    for mode in ("verify-lifecycle", "verify-full"):
        result = subprocess.run(
            [verifier, mode, root], text=True, capture_output=True, check=False
        )
        assert result.returncode == 0, result.stderr

    artifact = tmp_path / "artifact"
    artifact.mkdir()
    for relative in ("evidence", "client-image", "client-headers"):
        shutil.copytree(root / relative, artifact / relative)
    result = subprocess.run(
        [verifier, "verify-lifecycle", artifact],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [verifier, "verify-full", artifact],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0

    (artifact / "client-image" / "unlisted.txt").write_text(
        "not in the manifest\n", encoding="utf-8"
    )
    result = subprocess.run(
        [verifier, "verify-lifecycle", artifact],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_ci_has_one_status_only_release_preflight_before_parallel_flavors() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    jobs = workflow["jobs"]
    preflight = jobs["release-preflight"]
    commands = "\n".join(
        str(step.get("run", "")) for step in preflight["steps"]
    )
    assert commands.count("make build-image") == 0
    assert commands.count("make release-preflight") == 1
    make_targets = (ROOT / "mk" / "tests.mk").read_text()
    assert "release-preflight: build-image ##" in make_targets
    assert set(jobs["flavors"]["needs"]) == {
        "container_images",
        "discover-source",
        "lifecycle-decision",
        "fast",
        "release-preflight",
    }
    assert "outputs" not in preflight


def test_every_automatic_release_flavor_requires_kvm() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow_text = workflow_path.read_text()
    workflow = yaml.safe_load(workflow_text)
    steps = {step["name"]: step for step in workflow["jobs"]["flavors"]["steps"]}
    setup = steps["Prepare and verify hardware virtualization"]
    boot = steps["Boot and exercise the flavor"]
    cache_miss = "steps.restore_release_cache.outputs.cache-hit != 'true'"
    assert setup["if"] == cache_miss
    assert boot["if"] == cache_miss
    assert setup["run"] == (
        "make github-kvm-prepare GITHUB_QEMU_FLAVOR='${{ matrix.flavor }}'"
    )
    assert "QEMU_ACCEL=kvm" in boot["run"]

    setup_script = (ROOT / "scripts" / "github-prepare-kvm.sh").read_text()
    assert setup_script.count("sudo ") == 3
    assert "sudo apt-get update" in setup_script
    assert "DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1" in setup_script
    assert "qemu-system-x86 qemu-utils" in setup_script
    assert "sudo chmod 0666 /dev/kvm" in setup_script
    assert '"$DKC_ROOT/scripts/qemu-preflight.sh"' in setup_script
    assert '"$cpu_config" kvm "$flavor"' in setup_script
    assert "tcg" not in setup_script.lower()

    cpu_config = (ROOT / "config" / "qemu-cpus.env").read_text()
    assert "DKC_QEMU_CPU_V2='Nehalem-v1'" in cpu_config
    assert "DKC_QEMU_CPU_V3='Haswell-v2,-pcid'" in cpu_config
    assert "DKC_QEMU_CPU_V4='EPYC-Genoa-v1,-pcid,-la57'" in cpu_config

    preflight = (ROOT / "scripts" / "qemu-preflight.sh").read_text()
    makefile = (ROOT / "mk" / "vm.mk").read_text()
    assert '[ "$requested_accel" = kvm ]' in preflight
    assert "software emulation is not accepted" in preflight
    assert '-cpu "${model},enforce=on"' in preflight
    assert "probe_models tcg" not in preflight
    assert "using TCG" not in preflight
    assert "QEMU_ACCEL ?= kvm" in makefile
    assert "QEMU_PREFLIGHT_FLAVOR ?= all" in makefile
    assert '-cpu "${model},enforce=on"' in (
        ROOT / "scripts" / "qemu-boot.sh"
    ).read_text()

    todo = (ROOT / "TODO.md").read_text()
    assert "Periodic v4 qualification" in todo
    assert "Do not configure or select a self-hosted runner" in todo
    assert "not part of the automatic release matrix" in todo
    assert not (ROOT / ".github" / "workflows" / "qemu-diagnostics.yml").exists()
    assert not (ROOT / "scripts" / "qemu-host-diagnostics.sh").exists()


def test_release_build_handoff_separates_semantic_and_transport_cache_keys() -> None:
    workflow_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]
    decision = jobs["lifecycle-decision"]
    assert {
        "v2_cache_key",
        "v2_cache_transport_key",
        "v3_cache_key",
        "v3_cache_transport_key",
    } <= set(decision["outputs"])

    flavors = jobs["flavors"]
    steps = {step["name"]: step for step in flavors["steps"]}
    restore = steps["Restore an already accepted flavor"]
    save = steps["Save the newly accepted flavor"]
    expected_action = "@caa296126883cff596d87d8935842f9db880ef25"
    assert expected_action in restore["uses"]
    assert expected_action in save["uses"]
    assert "restore-keys" not in restore["with"]
    assert restore["with"]["key"] == "${{ env.GITHUB_RELEASE_CACHE_TRANSPORT_KEY }}"
    assert save["with"]["key"] == restore["with"]["key"]
    miss = "steps.restore_release_cache.outputs.cache-hit != 'true'"
    for name in (
        "Prepare and verify hardware virtualization",
        "Build and audit the flavor",
        "Build exact-source kernel selftests",
        "Boot and exercise the flavor",
        "Seal the accepted flavor handoff",
    ):
        assert steps[name]["if"] == miss
    assert steps["Save the newly accepted flavor"]["if"] == (
        "steps.restore_release_cache.outputs.cache-hit != 'true'"
    )
    assert "if" not in steps["Verify the accepted flavor handoff"]
    flavor_condition = str(flavors["if"])
    assert flavor_condition.startswith("always() &&")
    assert "needs.lifecycle-decision.outputs.build_required == 'true'" in (
        flavor_condition
    )
    assert "dkc-flavor-${{" not in workflow_text
    assert "dkc-kselftest-${{" not in workflow_text
    assert "dkc-qemu-${{" not in workflow_text

    matrix_steps = {step["name"]: step for step in jobs["package-matrix"]["steps"]}
    for flavor in ("v2", "v3"):
        restored = matrix_steps[f"Restore accepted {flavor} result"]
        assert expected_action in restored["uses"]
        assert restored["with"]["fail-on-cache-miss"] is True
        assert "restore-keys" not in restored["with"]
        assert restored["with"]["key"] == (
            "${{ needs.lifecycle-decision.outputs."
            + flavor
            + "_cache_transport_key }}"
        )
        assert f"Verify accepted {flavor} result" in matrix_steps
    package_command = matrix_steps["Reconcile and install-test all packages"]["run"]
    assert "MATRIX_V2=out/release-cache/v2/flavor" in package_command
    assert "MATRIX_V3=out/release-cache/v3/flavor" in package_command

    final = jobs["verify-published-state"]
    assert "permissions" not in final
    terminal = jobs["lifecycle-result"]
    assert terminal["permissions"] == {"actions": "write", "contents": "read"}
    cleanup = terminal["steps"][-1]
    assert cleanup["name"] == "Remove accepted release caches"
    assert cleanup["if"].startswith("success() &&")
    assert "decision == 'build'" in cleanup["if"]
    assert "decision == 'maintenance'" in cleanup["if"]
    assert "decision == 'no_op'" in cleanup["if"]
    assert cleanup["run"].startswith("make github-release-cache-delete ")


def test_pull_requests_run_full_non_publishing_qualification() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    jobs = workflow["jobs"]
    assert "github.event_name == 'pull_request'" in jobs["discover-source"]["if"]
    assert "github.event_name == 'pull_request'" in jobs["lifecycle-decision"]["if"]
    decision_steps = {
        step["name"]: step for step in jobs["lifecycle-decision"]["steps"]
    }
    qualification = decision_steps["Make the pull-request build qualification"]
    assert qualification["if"] == "github.event_name == 'pull_request'"
    assert qualification["run"].startswith("make github-pull-request-qualification ")
    assert decision_steps["Download authenticated state handoff"]["if"] == (
        "github.event_name != 'pull_request'"
    )

    flavor_condition = str(jobs["flavors"]["if"])
    assert flavor_condition.startswith("always() &&")
    for dependency in jobs["flavors"]["needs"]:
        assert f"needs.{dependency}.result == 'success'" in flavor_condition
    assert "build_required == 'true'" in flavor_condition

    package_job = jobs["package-matrix"]
    assert "environment" not in package_job
    assert "secrets." not in str(package_job)
    package_steps = {step["name"]: step for step in package_job["steps"]}
    repository = package_steps[
        "Qualify the complete repository with a disposable key"
    ]
    assert repository["if"] == "github.event_name == 'pull_request'"
    assert repository["run"].startswith("make github-apt-repository-qualify ")
    assert package_job["env"]["APT_CLIENT_IMAGE"] == (
        "${{ needs.container_images.outputs.apt_client_image }}"
    )

    for name in (
        "read-authoritative-state",
        "export-live-pool",
        "refresh-metadata",
        "current-main",
        "sign-repository",
        "verify-repository",
        "publish-repository",
        "verify-published-state",
        "lifecycle-result",
    ):
        assert "github.event_name != 'pull_request'" in str(
            jobs[name].get("if", "")
        ), name


def test_language_policy_does_not_decode_tracked_binary_artifacts() -> None:
    quality = (ROOT / "mk" / "quality.mk").read_text()
    assert "git -C $(DKC_ROOT) grep -I -lnP" in quality


def test_verified_clean_client_is_the_external_publication_boundary() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    jobs = workflow["jobs"]
    verification = jobs["verify-repository"]
    assert set(verification["needs"]) == {
        "container_images",
        "lifecycle-decision",
        "sign-repository",
    }
    assert "environment" not in verification
    verification_text = str(verification)
    assert "secrets." not in verification_text
    assert "make apt-repository-verify-decision" in verification_text
    assert "APT_CLIENT_IMAGE" in verification["env"]
    make_targets = (ROOT / "mk" / "build.mk").read_text()
    assert "image $(if $(filter all verify,$(APT_REPOSITORY_PHASE)),apt-client-image)" in make_targets
    assert "LIFECYCLE_DECISION_RESULT=out/ci-lifecycle" in verification_text

    signing = jobs["sign-repository"]
    assert "lifecycle-decision" in signing["needs"]
    assert "Download the lifecycle signing authorization" in str(signing)
    assert "LIFECYCLE_DECISION_RESULT=out/ci-lifecycle" in str(signing)

    signed_client = (
        ROOT / "scripts" / "in-container" / "test-signed-repository.sh"
    ).read_text()
    assert "dkc-linux-image-v2-amd64" in signed_client
    assert "dkc-linux-image-v3-amd64" in signed_client
    assert "apt-install-release-kernels.log" in signed_client
    assert "release_kernel_install=PASS" in signed_client

    publisher = jobs["publish-repository"]
    assert "verify-repository" in publisher["needs"]
    assert publisher["environment"]["name"] == "production-storage"
    assert publisher["permissions"] == {"actions": "read", "contents": "read"}
    assert "make github-storage-publish" in str(publisher)
    for name, job in jobs.items():
        if name not in {
            "read-authoritative-state",
            "export-live-pool",
            "publish-repository",
            "verify-published-state",
        }:
            assert "S3_ACCESS_KEY_ID" not in str(job)
            assert "S3_SECRET_ACCESS_KEY" not in str(job)


def test_storage_secrets_never_reach_image_resolution_or_current_main() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    )
    jobs = workflow["jobs"]
    for job_name in (
        "read-authoritative-state",
        "export-live-pool",
        "publish-repository",
        "verify-published-state",
    ):
        steps = jobs[job_name]["steps"]
        image_step = next(
            step
            for step in steps
            if step["name"] == "Fetch and verify the confined storage toolbox"
        )
        assert "env" not in image_step
        assert image_step["run"] == "make image"
        secret_steps = [step for step in steps if "S3_ENDPOINT" in str(step.get("env", {}))]
        assert len(secret_steps) == 1
        assert secret_steps[0]["run"].split()[1].startswith("github-storage-")

    signing = jobs["sign-repository"]["steps"]
    image_step = next(
        step
        for step in signing
        if step["name"] == "Fetch and verify the confined signing toolbox"
    )
    secret_step = next(step for step in signing if "APT_GPG_PASSPHRASE" in str(step))
    assert image_step["run"] == "make image"
    assert secret_step["run"].split()[1] == "github-apt-repository-sign"

    publisher_script = (ROOT / "scripts" / "storage-publish.sh").read_text()
    for name in (
        "S3_ENDPOINT",
        "S3_REGION",
        "S3_BUCKET",
        "S3_ADDRESSING_STYLE",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "S3_SESSION_TOKEN",
        "GITHUB_TOKEN",
    ):
        assert f"-u {name}" in publisher_script


def test_disposable_storage_qualification_is_local_only() -> None:
    assert not (ROOT / ".github" / "workflows" / "storage-integration.yml").exists()
    workflows = "\n".join(
        path.read_text() for path in (ROOT / ".github" / "workflows").glob("*.yml")
    )
    assert "make storage-disposable" not in workflows
    assert "_dkc-test/storage" not in workflows


def test_local_storage_credentials_and_recovery_are_file_confined() -> None:
    qualify = (ROOT / "scripts" / "storage-disposable.sh").read_text()
    cleanup = (ROOT / "scripts" / "storage-disposable-cleanup.sh").read_text()
    makefile = (ROOT / "mk" / "build.mk").read_text()
    assert "STORAGE_CONNECTION_FILE ?=" in makefile
    assert "storage-disposable-cleanup:" in makefile
    connection = (ROOT / "dkc" / "storage_connection.py").read_text()
    assert "connection input must not grant group or other permissions" in connection
    assert "materialize_connection" in connection
    assert "dkc::prepare_storage_connection" in qualify
    assert "dkc::prepare_storage_connection" in cleanup
    assert '--volume "$connection_file:/run/secrets/storage.json:ro"' in qualify
    assert '--volume "$connection_file:/run/secrets/storage.json:ro"' in cleanup
    assert "--env S3_" not in qualify
    assert "--env S3_" not in cleanup
    assert "run storage-disposable-cleanup for the retained result" in qualify


def test_every_authenticated_storage_container_checks_its_confinement() -> None:
    for relative in (
        "scripts/storage-state-read.sh",
        "scripts/storage-export-pool.sh",
        "scripts/storage-publish.sh",
        "scripts/storage-disposable.sh",
        "scripts/storage-disposable-cleanup.sh",
    ):
        script = (ROOT / relative).read_text()
        assert "--cap-drop=ALL --security-opt=no-new-privileges" in script
        assert "--log-driver=none" in script
        assert '^CapEff:[[:space:]]+0+$' in script
        assert '^NoNewPrivs:[[:space:]]+1$' in script
        assert "dkc::prepare_storage_connection" in script


def test_normal_build_uses_detected_cpus_without_synthetic_cgroup_caps() -> None:
    orchestrator = (ROOT / "scripts" / "build-one.sh").read_text()
    in_container = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    makefile = (ROOT / "mk" / "build.mk").read_text()
    assert "BUILD_JOBS ?= $(shell nproc)" in makefile
    assert "KERNEL_LTO ?= thin" in makefile
    assert "UPDATE_LATEST ?= 1" in makefile
    assert "BUILD_MEMORY" not in makefile
    assert "BUILD_CPUS" not in makefile
    assert "--memory=" not in orchestrator
    assert "--memory-swap=" not in orchestrator
    assert "--cpus=" not in orchestrator
    assert 'available_cpus="$(nproc)"' in in_container
    assert "available_cpus=${available_cpus}" in in_container
    assert "lto_mode=${lto_mode}" in in_container
    assert "cgroup_cpu_quota_us=${cpu_quota_us}" in in_container


def test_general_container_has_no_memory_or_cpu_cap_interface() -> None:
    script = (ROOT / "scripts" / "container-run.sh").read_text()
    assert "DKC_CONTAINER_MEMORY" not in script
    assert "DKC_CONTAINER_CPUS" not in script
    assert "--memory" not in script
    assert "--memory-swap" not in script
    assert "--cpus" not in script
    assert "hermetic | build | debug" not in script
    assert "--work-size" not in script
    assert "--pids-limit=1024" in script


def test_qemu_boot_uses_immutable_overlays_and_bounded_result_disks() -> None:
    orchestrator = (ROOT / "scripts" / "qemu-boot.sh").read_text()
    preparer_path = ROOT / "scripts" / "in-container" / "prepare-qemu-inputs.sh"
    guest_path = ROOT / "tests" / "integration" / "qemu" / "guest-validate.sh"
    guest = guest_path.read_text()
    makefile = (ROOT / "mk" / "vm.mk").read_text()
    assert os.access(preparer_path, os.X_OK)
    assert os.access(guest_path, os.X_OK)
    assert "qemu-system-x86_64" in orchestrator
    assert "libvirt" not in orchestrator
    assert "virsh" not in orchestrator
    assert "-b \"$base_image\" \"$overlay\" 16G" in orchestrator
    assert "readonly=on" in orchestrator
    assert "QEMU base image must be read-only" in orchestrator
    assert "immutable QEMU base image changed during validation" in orchestrator
    assert '.["full-backing-filename"]' in orchestrator
    assert "-device VGA" in orchestrator
    assert "results.img" in orchestrator
    assert "debugfs" in orchestrator
    assert "timeout --signal=TERM" in orchestrator
    assert "selected_flavors" not in orchestrator
    assert "for flavor in $selected" not in orchestrator
    assert "failures=" not in orchestrator
    assert "input_volumes" not in orchestrator
    assert "prepare_arguments" not in orchestrator
    assert "flavor=${flavor}" in orchestrator
    assert "lto_mode=${lto_mode}" in orchestrator
    assert "make qemu-boot" not in guest
    assert "dpkg --unpack" in guest
    assert "dpkg --purge" in guest
    assert "gpg" not in preparer_path.read_text()
    assert "dpkg-scanpackages" not in (
        preparer_path
    ).read_text()
    assert "modprobe dkc_fixture" in guest
    assert "modprobe dummy" in guest
    assert "qemu-boot-flavor: image vm-base-image" in makefile
    assert "kselftest-flavor: build-image" in makefile
    assert "run_kselftest_profile" in guest
    assert 'chmod -R a+rX "$work"' in guest
    assert 'chmod -R a+rX "$install_root"' in (
        ROOT / "scripts" / "in-container" / "build-kselftest.sh"
    ).read_text()
    assert "KSELFTEST_RESULT" in makefile
    assert "KSELFTEST_SOURCE_RESULT" not in makefile
    assert "/input/kselftest" in orchestrator
    assert ".lto_mode == $identity[0].lto_mode" in preparer_path.read_text()
    assert ".lto_mode == $attestation[0].lto_mode" in preparer_path.read_text()
    assert "/input/source" not in (
        ROOT / "scripts" / "build-kselftest-flavor.sh"
    ).read_text()
    assert "kselftest-source-patches.sha256" in (
        ROOT / "scripts" / "in-container" / "build-kselftest-flavor.sh"
    ).read_text()
    assert '"lto_mode": lto_mode' in (
        ROOT / "scripts" / "in-container" / "build-kselftest.sh"
    ).read_text()
    assert "kselftest-nr-open.env" in guest
    assert "kselftest-summary.env" in guest
    assert 'if timeout --signal=TERM --kill-after=15s "${aggregate}s"' in guest
    assert '"$work/run_kselftest.sh" -o "$per_test" -p "$logs"' in guest
    assert "kselftest-per-test-logs.tar.xz" in guest
    assert "kselftest-skips.log" in guest
    assert "nested_skip=${nested_skip}" in guest
    assert "$(sha256sum \"$raw\" | awk '{print $1}')" in guest
    assert "$(sha256sum \"$serial\" | awk '{print $1}')" in orchestrator


def test_package_audit_fails_on_lintian_errors_or_execution_failure() -> None:
    script = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    assert "lintian --display-info --pedantic --fail-on error" in script
    assert 'if [ "$lintian_rc" -ne 0 ]; then' in script
    assert "timeout --signal=TERM --kill-after=5m 5h" in script


def test_flavor_build_is_explicit_and_command_audit_is_mandatory() -> None:
    makefile = (ROOT / "mk" / "build.mk").read_text()
    orchestrator = (ROOT / "scripts" / "build-one.sh").read_text()
    run = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    finalizer = (ROOT / "scripts" / "in-container" / "finalize-one-build.sh").read_text()
    assert "FLAVOR ?= v2" in makefile
    assert "build-flavor:" in makefile
    assert "flavor must be v2, v3, or v4" in orchestrator
    assert "audit-kbuild-commands.py" in run
    assert "audit-kernel-simd.py" in run
    assert "kernel-simd-audit.json" in finalizer
    assert "kbuild-command-audit.json" in finalizer
    assert "build-kselftest.sh" not in run
    assert "kselftest-build.json" not in finalizer
    assert '"$lto_mode"' in run
    assert '--lto-mode "$lto_mode"' in run
    assert '--lto-mode "$lto_mode"' in finalizer
    assert 'if [ "$update_latest" = 1 ]; then' in orchestrator
    assert "prepare-attestation-replay.sh" in run
    assert run.index("prepare-attestation-replay.sh") < run.index("attest-one-build.py")
    assert run.index("audit-kernel-simd.py") < run.index("attest-one-build.py")


def test_build_compresses_the_large_log_without_losing_its_digest() -> None:
    run = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    finalizer = (ROOT / "scripts" / "in-container" / "finalize-one-build.sh").read_text()
    assert "sha256sum build.log >build.log.sha256" in run
    assert "xz --threads=1 --check=sha256 -1 build.log" in run
    assert 'xz --decompress --stdout "$result/evidence/build.log.xz"' in finalizer
    assert "sha256sum --check ../evidence/artifacts.sha256" in finalizer
    assert "missing required build evidence" in finalizer


def test_completed_compilation_exports_replayable_attestation_inputs() -> None:
    prepare = (
        ROOT / "scripts" / "in-container" / "prepare-attestation-replay.sh"
    ).read_text()
    finalizer = (ROOT / "scripts" / "in-container" / "finalize-one-build.sh").read_text()
    replay = (ROOT / "scripts" / "in-container" / "reattest-flavor.sh").read_text()
    makefile = (ROOT / "mk" / "build.mk").read_text()
    assert "--strip-debug" in prepare
    assert "executable_sections_comparison" in prepare
    assert "verify-replay-elf.py" in prepare
    for name in (
        "attestation-replay/vmlinux.zst",
        "attestation-replay/System.map",
        "attestation-replay/config",
        "attestation-replay/build-tree-inventory.json",
        "attestation-replay/derived-fpu-symbols.json",
        "attestation-replay/executable-sections.json",
        "attestation-replay/kernel-simd-observations.json.xz",
    ):
        assert name in finalizer
    assert "retained SIMD observations do not reproduce" in finalizer
    assert "retained inputs do not reproduce package attestation" in finalizer
    assert finalizer.count("1>&2") >= 2
    assert "--observations-input" in replay
    assert "attest-one-build.py" in replay
    assert "--replay-evidence" in replay
    assert "sha256sum --check evidence.sha256" in replay
    assert "build-tree-inventory.json" in prepare
    assert "kbuild-commands.tsv.xz" in prepare
    assert 'if [ "$mode" = observations ]; then' in replay
    assert "zstd -q -d -c" in replay
    assert "reattest-flavor:" in makefile


def test_final_export_recovery_is_narrow_and_reuses_the_finalizer() -> None:
    recovery = (ROOT / "scripts" / "recover-flavor-export.sh").read_text()
    makefile = (ROOT / "mk" / "build.mk").read_text()
    assert "failure_phase=final-export" in recovery
    assert "sha256sum --check evidence.sha256" in recovery
    assert "sha256sum --check /input/result/evidence/artifacts.sha256" in recovery
    assert "finalize-one-build.sh" in recovery
    assert 'podman image exists "$expected_image_id"' in recovery
    assert "compilation_reused=true" in recovery
    assert "recover-flavor-export:" in makefile


def test_attestation_recovery_replays_every_recoverable_gate() -> None:
    recovery = (ROOT / "scripts" / "recover-flavor-attestation.sh").read_text()
    makefile = (ROOT / "mk" / "build.mk").read_text()
    assert "failure_phase=offline-build" in recovery
    assert "sha256sum --check evidence.sha256" in recovery
    assert "sha256sum --check /input/result/evidence/artifacts.sha256" in recovery
    assert "sha256sum --check /input/result/evidence/failure-artifacts.sha256" in recovery
    assert 'values["lintian_rc"] != "0"' in recovery
    assert "reattest-flavor.sh" in recovery
    assert 'podman image exists "$expected_image_id"' in recovery
    assert "observations" in recovery
    assert "observations 1>&2" in recovery
    assert recovery.index("reattest-flavor.sh") < recovery.index("finalize-one-build.sh")
    assert "compilation_reused=true" in recovery
    assert "complete_simd_observations_reused=true" in recovery
    assert "recover-flavor-attestation:" in makefile


def test_build_evidence_hashes_the_streamed_repository_inputs() -> None:
    stage = (ROOT / "scripts" / "in-container" / "stage-one-build.sh").read_text()
    run = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    finalize = (ROOT / "scripts" / "in-container" / "finalize-one-build.sh").read_text()
    assert 'find . -type f -print0 | sort -z | xargs -0 sha256sum' in stage
    assert "repository-inputs.sha256" in run
    assert "repository-inputs.sha256" in finalize
    for name in ("build-image-debs.tsv", "staging-apt-indexes.sha256"):
        assert name in run
        assert name in finalize
    orchestrator = (ROOT / "scripts" / "build-one.sh").read_text()
    assert "build-image-provenance.env" in orchestrator
    assert "registry_manifest_digest=" in orchestrator
    assert "config_digest=" in orchestrator
    assert "bundle_input_sha256=" in orchestrator
    assert "bundle_generation=" in orchestrator
    assert "build-image-provenance.env" in finalize
    policy = (ROOT / "dkc" / "buildpolicy.py").read_text()
    assert '"scripts/build-one.sh"' in policy
    assert '"scripts/in-container/build-source-package.sh"' in policy


def test_every_expensive_flavor_phase_exports_bounded_failure_evidence() -> None:
    orchestrator = (ROOT / "scripts" / "build-one.sh").read_text()
    for phase in (
        "source-staging",
        "identity-staging",
        "offline-build",
        "final-export",
        "host-acceptance",
    ):
        assert f"export_failed_evidence {phase}" in orchestrator
    assert "failure_phase=${failure_phase}" in orchestrator
    assert "evidence.sha256" in orchestrator
    assert "source-staging.log" in orchestrator
    assert "build-controller.log" in orchestrator
    assert "tar --create --file=- evidence artifacts" in orchestrator
    assert "tar --create --file=- evidence artifacts source" in orchestrator
    assert "test -d /work/source-package" in orchestrator
    assert "failure-artifacts.sha256" in orchestrator
    finalizer = (ROOT / "scripts" / "in-container" / "finalize-one-build.sh").read_text()
    assert "evidence.sha256" not in finalizer
    assert orchestrator.rindex("build_image_bytes=%s") < orchestrator.rindex(
        ') >"$export_stage/evidence/evidence.sha256"'
    )


def test_resource_observation_cannot_abort_an_active_kernel_build() -> None:
    run = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    assert 'if sampled_root_used="$(' in run
    assert "root_sample_errors=$((root_sample_errors + 1))" in run
    assert "root_sample_errors=${root_sample_errors}" in run
    assert "du --summarize" not in run
    assert "controller-error.env" in run
    assert "post-build-gates.env" in run
    assert run.index('cat >"$evidence/capacity.env"') < run.rindex(
        'post-build gates failed:'
    )


def test_all_source_downloaders_stream_and_retry_transient_failures() -> None:
    for relative in (
        "scripts/in-container/refresh-overlay-patches.sh",
        "scripts/in-container/stage-one-build.sh",
        "scripts/in-container/verify-overlay.sh",
    ):
        downloader = (ROOT / relative).read_text()
        assert "for attempt in range(1, 5)" in downloader
        assert "response.read(1024 * 1024)" in downloader
        assert "partial.unlink(missing_ok=True)" in downloader
        assert "time.sleep(attempt)" in downloader
        assert 'headers={"User-Agent":' in downloader


def test_runtime_source_paths_come_from_the_authenticated_inventory() -> None:
    stage = (ROOT / "scripts" / "in-container" / "stage-one-build.sh").read_text()
    run = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    source_package = (
        ROOT / "scripts" / "in-container" / "build-source-package.sh"
    ).read_text()
    orchestrator = (ROOT / "scripts" / "build-one.sh").read_text()
    discovery = (ROOT / "dkc" / "source_discovery.py").read_text()

    assert '"DKC_{prefix}_NAME"' in discovery
    assert '"$inputs/$orig_name"' in stage
    assert '"/work/inputs/$dsc_name"' in run
    assert 'orig_input="$inputs/$orig_name"' in source_package
    for source in (stage, run, source_package, orchestrator):
        assert "linux-7.1.7" not in source
        assert "linux_7.1.7.orig.tar.xz" not in source


def test_selected_kbuild_target_declares_and_checks_its_python_helper() -> None:
    generator = (ROOT / "scripts" / "in-container" / "generate-overlay-patches.py").read_text()
    containerfile = (ROOT / "container" / "Containerfile.build").read_text()
    verifier = (ROOT / "scripts" / "in-container" / "verify-overlay.sh").read_text()
    run = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    assert '"dh-python <!pkg.linux.notools>"' in generator
    assert '"dh-python <!pkg.dkc.nokbuild>"' in generator
    assert "\n\t\tdh-python \\\n" in containerfile
    assert "command -v dh_python3" in verifier
    assert "command -v dh_python3" in run
    assert "selected binary target set differs before compilation" in run
    assert "selected-packages.txt" in run


def test_kernel_removal_hook_runs_after_the_binary_payload_is_removed() -> None:
    generator = (ROOT / "scripts" / "in-container" / "generate-overlay-patches.py").read_text()
    audit = (ROOT / "scripts" / "in-container" / "audit-package-matrix.py").read_text()
    assert 'add_file(root, "debian/templates/binary.postrm.in", BINARY_POSTRM)' in generator
    assert generator.count("linux-run-hooks image postrm") >= 3
    assert 'if [ "$1" = remove ]; then' in generator
    assert "does not defer removal hooks to the binary package" in audit


def test_build_fails_cheaply_on_tool_or_kconfig_drift() -> None:
    run = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    stage = (ROOT / "scripts" / "in-container" / "stage-one-build.sh").read_text()
    attestation = (ROOT / "scripts" / "in-container" / "attest-one-build.py").read_text()
    assert 'scripts/min-tool-version.sh "$name"' in run
    assert 'expected_tool="/usr/bin/${tool}"' in run
    assert 'readlink -f "$actual_tool"' in run
    assert "tool-minimums.env" in run
    assert "grep -qx 'CONFIG_RUST=y'" in run
    assert 'cmp -s "$config_preflight/.config" "$final_config"' in run
    assert '"CONFIG_RUST": "y"' in attestation
    assert "rust_source=debian" in stage

    generator = (ROOT / "scripts" / "in-container" / "generate-overlay-patches.py").read_text()
    attestation = (ROOT / "scripts" / "in-container" / "attest-one-build.py").read_text()
    assert "if grep -qx 'CONFIG_DEBUG_INFO_BTF=y'" in generator
    assert "headers package contains a vmlinux payload while BTF is disabled" in attestation
    assert '"btf_policy": btf_policy' in attestation


def test_release_build_has_one_debian_rust_toolchain_path() -> None:
    containerfile = (ROOT / "container" / "Containerfile.build").read_text()
    makefile = (ROOT / "mk" / "container.mk").read_text()
    assert '-t "${DEBIAN_SUITE}-backports" rustc rust-src' in containerfile
    assert "rustc_llvm_major" in containerfile
    for marker in ("RUST_SOURCE", "RUST_VERSION", "rustup-init"):
        assert marker not in containerfile
        assert marker not in makefile
    assert "build-image-rust-pinned" not in makefile


def test_publication_changelog_drives_the_reproducible_timestamp() -> None:
    identity = (ROOT / "scripts" / "in-container" / "prepare-build-identity.py").read_text()
    run = (ROOT / "scripts" / "in-container" / "run-one-build.sh").read_text()
    assert 'f"dkc-linux ({package_version}) trixie; urgency=medium' in identity
    assert "publication_source_date_epoch" in identity
    assert 'dpkg-parsechangelog -l"$source/debian/changelog" -STimestamp' in run


def test_build_image_proves_bindgen_loaded_the_selected_libclang() -> None:
    containerfile = (ROOT / "container" / "Containerfile.build").read_text()
    assert "LD_DEBUG=libs bindgen" in containerfile
    assert 'calling init: .*/libclang-${LLVM_MAJOR}' in containerfile
    assert "calling init: .*/libclang-19" in containerfile


def test_package_client_uses_exact_installed_and_autoremove_sets() -> None:
    orchestrator = (ROOT / "scripts" / "package-matrix.sh").read_text()
    matrix_verifier = (
        ROOT / "scripts" / "package-matrix-manifest.sh"
    ).read_text()
    assembler = (
        ROOT / "scripts" / "in-container" / "assemble-apt-repository.sh"
    ).read_text()
    repository = (ROOT / "scripts" / "apt-repository.sh").read_text()
    signer = (ROOT / "scripts" / "sign-apt-repository.sh").read_text()
    verifier = (ROOT / "scripts" / "verify-apt-repository.sh").read_text()
    client = (ROOT / "scripts" / "in-container" / "test-package-client.sh").read_text()
    assert orchestrator.count("dkc::archive_worktree |") == 2
    assert '--volume "$DKC_ROOT:/repo:ro"' not in orchestrator
    assert "--tmpfs=/work:rw,exec,nosuid,nodev,size=512m,mode=1777" in orchestrator
    assert "package reconciliation failed with rc=" in orchestrator
    assert "tar --extract --file=- --directory=/repo" in orchestrator
    assert 'package-matrix-manifest.sh" write "$stage"' in orchestrator
    assert 'package-matrix-manifest.sh" verify-full "$stage"' in orchestrator
    assert 'find "${scopes[@]}" -type f' in matrix_verifier
    assert "flat-repository.sha256" in matrix_verifier
    assert "cmp -" in matrix_verifier
    assert "package-matrix-manifest.sh verify-full" in assembler
    assert "build-local-signed-repository.sh" not in orchestrator
    assert "test-signed-repository.sh" not in orchestrator
    assert "assemble | sign | verify | all | qualify" in repository
    assert "ephemeral APT signing is forbidden in GitHub Actions" in repository
    assert "must keep APT assembly, signing, and verification in separate jobs" in repository
    assert "repository qualification requires an ephemeral signing key" in repository
    assert "DKC_APT_PULL_REQUEST_QUALIFICATION=1" in repository
    assert "DKC_APT_PULL_REQUEST_QUALIFICATION" in signer
    github_make = (ROOT / "mk" / "github.mk").read_text()
    assert "github-apt-repository-qualify: image apt-client-image" in github_make
    assert "DKC_APT_EPHEMERAL_SIGNING=1" in github_make
    assert "APT_GPG_SIGNING_SUBKEY_B64" in signer
    assert "--network=none" in signer
    assert "APT_REPOSITORY_IMAGE_PREREQUISITES" in (
        ROOT / "mk" / "build.mk"
    ).read_text()
    assert "test-signed-repository.sh" in verifier
    assert "python3" not in client
    assert "/matrix/repository/Packages" in client
    assert client.count("file:/matrix/repository | file:/matrix/repository/*") == 2
    assert "${db:Status-Status}" in client
    assert '$2 == "installed"' in client
    assert '[ "${#initial_removals[@]}" -eq 0 ]' in client
    assert "proposed_removals" in client
    assert "proposed_kernel_removals" not in client
    assert "exact v3 versioned closure" in client
    assert 'test ! -e "/boot/initrd.img-${v3_krel}"' in client
    assert 'grep -q \'^Remv \' "/evidence/autoremove-after-${mode}.txt"' in client


def test_secret_bearing_prepared_targets_do_not_resolve_images() -> None:
    expected_scripts = {
        "github-apt-repository-sign": "scripts/apt-repository.sh",
        "github-storage-export-pool": "scripts/storage-export-pool.sh",
        "github-storage-publish": "scripts/storage-publish.sh",
        "github-storage-state-read": "scripts/storage-state-read.sh",
    }
    for target, expected_script in expected_scripts.items():
        result = subprocess.run(
            ["make", "-n", target],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
        )
        assert "podman build" not in result.stdout
        assert expected_script in result.stdout
        assert "scripts/container-images.sh" not in result.stdout


@pytest.mark.parametrize(
    "entrypoint",
    (
        "scripts/github-ci.py",
        "scripts/release-gate.py",
        "scripts/storage-connection.py",
    ),
)
def test_host_python_entrypoints_import_checkout_package_without_pythonpath(
    entrypoint: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(ROOT / entrypoint), "--help"],
        cwd="/tmp",
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_signed_repository_uses_a_dedicated_pinned_client_image() -> None:
    containerfile = (ROOT / "container" / "Containerfile.apt-client").read_text()
    makefile = (ROOT / "mk" / "container.mk").read_text()
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "FROM ${BASE_IMAGE}" in containerfile
    for package in (
        "dpkg-dev",
        "gpgv",
        "initramfs-tools",
        "kmod",
        "linux-base",
        "python3-jsonschema",
        "xz-utils",
    ):
        assert package in containerfile
    assert "apt-client-image:" in makefile
    assert "APT_CLIENT_IMAGE: ${{ needs.container_images.outputs.apt_client_image }}" in workflow
    build_targets = (ROOT / "mk" / "build.mk").read_text()
    assert "apt-repository: $(APT_REPOSITORY_IMAGE_PREREQUISITES)" in build_targets
