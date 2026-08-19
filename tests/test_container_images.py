"""Container image bundle policy and host-boundary tests."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from collections.abc import Iterator

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
HELPER = ROOT / "scripts" / "container-images.sh"
BASE_IMAGE = (ROOT / "config" / "base-image.lock").read_text().strip()
INPUT_SHA = "a" * 64
GENERATION = "test-123"
REVISION = "b" * 40


@pytest.fixture
def executable_tmp_path(tmp_path: pathlib.Path) -> Iterator[pathlib.Path]:
    """Provide a disposable executable directory outside the noexec /tmp."""
    directory = ROOT / ".dkc-run" / "test-bin" / tmp_path.name
    directory.mkdir(parents=True)
    try:
        yield directory
    finally:
        shutil.rmtree(directory)


def _workflow_events(workflow: dict[object, object]) -> dict[str, object]:
    value = workflow.get("on", workflow.get(True))
    assert isinstance(value, dict)
    return value


def _run_helper(
    *arguments: str,
    path: pathlib.Path | None = None,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["DKC_RUN_ID"] = "20260816T000000Z-1234abcd"
    if path is not None:
        environment["PATH"] = f"{path}:{environment['PATH']}"
    if extra_environment:
        environment.update(extra_environment)
    return subprocess.run(
        [HELPER, *arguments],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def _install_fake_podman(directory: pathlib.Path) -> pathlib.Path:
    fake = directory / "podman"
    fake.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\\n' "$*" >>"${FAKE_PODMAN_LOG:?}"
if [ "$1 $2" = "image exists" ]; then
    [ "${FAKE_MISSING_IMAGE:-}" != "$3" ]
    exit
fi
if [ "$1" = pull ]; then
    [ "${FAKE_PULL_FAILURE:-}" != "$2" ]
    exit
fi
if [ "$1" = push ] || [ "$1" = build ]; then
    exit 0
fi
if [ "$1 $2" = "image inspect" ]; then
    image="$3"
    format="$5"
    case "$image" in
        *dkc-toolbox*) role="${FAKE_TOOLBOX_ROLE:-toolbox}"; digest="sha256:$(printf '1%.0s' {1..64})" ;;
        *dkc-kernel-build*|*dkc-build:*) role="${FAKE_BUILD_ROLE:-kernel-build}"; digest="sha256:$(printf '2%.0s' {1..64})" ;;
        *dkc-apt-client*) role="${FAKE_CLIENT_ROLE:-apt-client}"; digest="sha256:$(printf '3%.0s' {1..64})" ;;
        *) role="${FAKE_ROLE:-toolbox}"; digest="sha256:$(printf '4%.0s' {1..64})" ;;
    esac
    case "$role" in
        toolbox) generation="${FAKE_TOOLBOX_GENERATION:-${FAKE_GENERATION:?}}" ;;
        kernel-build) generation="${FAKE_BUILD_GENERATION:-${FAKE_GENERATION:?}}" ;;
        apt-client) generation="${FAKE_CLIENT_GENERATION:-${FAKE_GENERATION:?}}" ;;
    esac
    case "$format" in
        '{{.Os}}/{{.Architecture}}') echo linux/amd64 ;;
        '{{.Digest}}') echo "$digest" ;;
        *image-role*) echo "$role" ;;
        *bundle-input-sha256*)
            case "$role" in
                toolbox) echo "${FAKE_TOOLBOX_INPUT_SHA:-${FAKE_INPUT_SHA:?}}" ;;
                kernel-build) echo "${FAKE_BUILD_INPUT_SHA:-${FAKE_INPUT_SHA:?}}" ;;
                apt-client) echo "${FAKE_CLIENT_INPUT_SHA:-${FAKE_INPUT_SHA:?}}" ;;
            esac
            ;;
        *bundle-generation*) echo "$generation" ;;
        *base-image*) echo "${FAKE_BASE_IMAGE:?}" ;;
        *llvm-major*) echo "${FAKE_LLVM_MAJOR:?}" ;;
        *org.opencontainers.image.source*) echo https://github.com/kogeler/dkc-linux ;;
        *org.opencontainers.image.revision*) echo "${FAKE_REVISION:?}" ;;
        *) echo "unsupported inspect format: $format" >&2; exit 2 ;;
    esac
    exit 0
fi
if [ "$1" = run ]; then
    echo 'toolchain smoke check'
    exit 0
fi
echo "unsupported fake podman command: $*" >&2
exit 2
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return fake


def _fake_environment(log: pathlib.Path) -> dict[str, str]:
    return {
        "FAKE_PODMAN_LOG": str(log),
        "FAKE_INPUT_SHA": INPUT_SHA,
        "FAKE_GENERATION": GENERATION,
        "FAKE_BASE_IMAGE": BASE_IMAGE,
        "FAKE_LLVM_MAJOR": "21",
        "FAKE_REVISION": REVISION,
    }


def test_input_inventory_is_complete_sorted_and_matches_workflow_filters() -> None:
    content = _run_helper("paths", "content")
    triggers = _run_helper("paths", "trigger")
    assert content.returncode == 0, content.stderr
    assert triggers.returncode == 0, triggers.stderr
    content_paths = content.stdout.splitlines()
    trigger_paths = triggers.stdout.splitlines()
    assert content_paths == sorted(set(content_paths))
    assert trigger_paths == sorted(set(trigger_paths))
    assert set(content_paths) < set(trigger_paths)
    assert not any(path.startswith("tests/") for path in trigger_paths)

    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "container-images.yml").read_text()
    )
    events = _workflow_events(workflow)
    assert set(events["push"]["paths"]) == set(trigger_paths)
    assert set(events["pull_request"]["paths"]) == set(trigger_paths)
    assert events["push"]["branches"] == ["main"]
    assert events["schedule"] == [{"cron": "0 9 * * 6"}]
    assert events["workflow_dispatch"] is None


def test_bundle_fingerprint_is_deterministic_and_covers_effective_arguments() -> None:
    first = _run_helper("fingerprint", BASE_IMAGE, "21")
    second = _run_helper("fingerprint", BASE_IMAGE, "21")
    changed = _run_helper("fingerprint", BASE_IMAGE, "22")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert changed.returncode == 0, changed.stderr
    assert len(first.stdout.strip()) == 64
    assert first.stdout == second.stdout
    assert first.stdout != changed.stdout


def test_make_defaults_to_build_and_registry_mode_has_no_build_recipe() -> None:
    local = subprocess.run(
        ["make", "-n", "image"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "container-images.sh build toolbox" in local.stdout

    digest = f"ghcr.io/kogeler/dkc-toolbox@sha256:{'0' * 64}"
    registry = subprocess.run(
        [
            "make",
            "-n",
            "image",
            "DKC_IMAGE_MODE=registry",
            f"TOOLBOX_IMAGE={digest}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "container-images.sh ensure toolbox" in registry.stdout
    assert "container-images.sh build" not in registry.stdout

    registry_fast = subprocess.run(
        [
            "make",
            "-n",
            "fast",
            "DKC_IMAGE_MODE=registry",
            f"TOOLBOX_IMAGE={digest}",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    assert "container-images.sh ensure toolbox" in registry_fast.stdout
    assert "container-images.sh build" not in registry_fast.stdout

    invalid = subprocess.run(
        ["make", "-n", "image", "DKC_IMAGE_MODE=automatic"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert invalid.returncode != 0
    assert "must be exactly build or registry" in invalid.stderr

    bundle = subprocess.run(
        ["make", "-n", "container-images"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    generation_lines = [
        line.strip().strip("'")
        for line in bundle.stdout.splitlines()
        if line.strip().startswith("'local-")
    ]
    assert len(generation_lines) == 3
    assert len(set(generation_lines)) == 1
    fingerprint = _run_helper("fingerprint", BASE_IMAGE, "21").stdout.strip()
    assert f"local-{fingerprint}" in generation_lines[0]


def test_registry_ensure_rejects_mutable_but_accepts_any_published_generation(
    tmp_path: pathlib.Path,
    executable_tmp_path: pathlib.Path,
) -> None:
    _install_fake_podman(executable_tmp_path)
    log = tmp_path / "podman.log"
    environment = _fake_environment(log)
    mutable = _run_helper(
        "ensure",
        "toolbox",
        "ghcr.io/kogeler/dkc-toolbox:latest",
        path=executable_tmp_path,
        extra_environment=environment,
    )
    assert mutable.returncode != 0
    assert "immutable canonical GHCR digest" in mutable.stderr

    digest = f"ghcr.io/kogeler/dkc-toolbox@sha256:{'1' * 64}"
    current = _run_helper(
        "ensure",
        "toolbox",
        digest,
        path=executable_tmp_path,
        extra_environment=environment,
    )
    assert current.returncode == 0, current.stderr


@pytest.mark.parametrize(
    "override",
    (
        {"FAKE_TOOLBOX_ROLE": "apt-client"},
        {"FAKE_INPUT_SHA": "not-a-digest"},
        {"FAKE_BASE_IMAGE": "debian:latest"},
        {"FAKE_LLVM_MAJOR": "unknown"},
    ),
)
def test_registry_ensure_rejects_wrong_bundle_metadata(
    override: dict[str, str],
    tmp_path: pathlib.Path,
    executable_tmp_path: pathlib.Path,
) -> None:
    _install_fake_podman(executable_tmp_path)
    environment = _fake_environment(tmp_path / "podman.log")
    environment.update(override)
    digest = f"ghcr.io/kogeler/dkc-toolbox@sha256:{'1' * 64}"
    result = _run_helper(
        "ensure",
        "toolbox",
        digest,
        path=executable_tmp_path,
        extra_environment=environment,
    )
    assert result.returncode != 0
    assert "failed published-image verification" in result.stderr


def test_registry_ensure_rejects_an_absent_digest(
    tmp_path: pathlib.Path,
    executable_tmp_path: pathlib.Path,
) -> None:
    _install_fake_podman(executable_tmp_path)
    digest = f"ghcr.io/kogeler/dkc-toolbox@sha256:{'1' * 64}"
    environment = _fake_environment(tmp_path / "podman.log")
    environment["FAKE_MISSING_IMAGE"] = digest
    environment["FAKE_PULL_FAILURE"] = digest
    result = _run_helper(
        "ensure",
        "toolbox",
        digest,
        path=executable_tmp_path,
        extra_environment=environment,
    )
    assert result.returncode != 0
    assert "pulling immutable toolbox image" in result.stderr


def test_publication_is_rejected_outside_canonical_main_actions(
    tmp_path: pathlib.Path,
    executable_tmp_path: pathlib.Path,
) -> None:
    _install_fake_podman(executable_tmp_path)
    result = _run_helper(
        "push-bundle",
        "localhost/dkc-toolbox:latest",
        "localhost/dkc-build:llvm21",
        "localhost/dkc-apt-client:latest",
        BASE_IMAGE,
        "21",
        INPUT_SHA,
        GENERATION,
        "ghcr.io/kogeler/dkc-toolbox:latest",
        "ghcr.io/kogeler/dkc-kernel-build:latest",
        "ghcr.io/kogeler/dkc-apt-client:latest",
        path=executable_tmp_path,
        extra_environment=_fake_environment(tmp_path / "podman.log"),
    )
    assert result.returncode != 0
    assert "restricted to GitHub Actions" in result.stderr


def test_canonical_publisher_moves_exactly_the_three_latest_tags(
    tmp_path: pathlib.Path,
    executable_tmp_path: pathlib.Path,
) -> None:
    _install_fake_podman(executable_tmp_path)
    log = tmp_path / "podman.log"
    environment = _fake_environment(log)
    environment.update(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "kogeler/dkc-linux",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "schedule",
            "GITHUB_SHA": REVISION,
        }
    )
    result = _run_helper(
        "push-bundle",
        "localhost/dkc-toolbox:latest",
        "localhost/dkc-build:llvm21",
        "localhost/dkc-apt-client:latest",
        BASE_IMAGE,
        "21",
        INPUT_SHA,
        GENERATION,
        "ghcr.io/kogeler/dkc-toolbox:latest",
        "ghcr.io/kogeler/dkc-kernel-build:latest",
        "ghcr.io/kogeler/dkc-apt-client:latest",
        path=executable_tmp_path,
        extra_environment=environment,
    )
    assert result.returncode == 0, result.stderr
    pushes = [line for line in log.read_text().splitlines() if line.startswith("push ")]
    assert pushes == [
        "push localhost/dkc-toolbox:latest docker://ghcr.io/kogeler/dkc-toolbox:latest",
        "push localhost/dkc-build:llvm21 docker://ghcr.io/kogeler/dkc-kernel-build:latest",
        "push localhost/dkc-apt-client:latest docker://ghcr.io/kogeler/dkc-apt-client:latest",
    ]


def test_resolver_emits_only_one_generation_of_immutable_references(
    tmp_path: pathlib.Path,
    executable_tmp_path: pathlib.Path,
) -> None:
    _install_fake_podman(executable_tmp_path)
    log = tmp_path / "podman.log"
    output = tmp_path / "output.env"
    result = _run_helper(
        "resolve",
        "",
        "2",
        "1",
        str(output),
        "ghcr.io/kogeler/dkc-toolbox:latest",
        "ghcr.io/kogeler/dkc-kernel-build:latest",
        "ghcr.io/kogeler/dkc-apt-client:latest",
        path=executable_tmp_path,
        extra_environment=_fake_environment(log),
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["bundle_input_sha256"] == INPUT_SHA
    assert values["bundle_generation"] == GENERATION
    assert values["toolbox_image"].startswith(
        "ghcr.io/kogeler/dkc-toolbox@sha256:"
    )
    assert values["build_image"].startswith(
        "ghcr.io/kogeler/dkc-kernel-build@sha256:"
    )
    assert values["apt_client_image"].startswith(
        "ghcr.io/kogeler/dkc-apt-client@sha256:"
    )
    assert ":latest" not in output.read_text()


@pytest.mark.parametrize(
    "override,expected_generation",
    (
        ({"FAKE_BUILD_GENERATION": "another-generation"}, ""),
        ({"FAKE_BUILD_INPUT_SHA": "c" * 64}, ""),
        ({"FAKE_PULL_FAILURE": "ghcr.io/kogeler/dkc-apt-client:latest"}, ""),
        ({"FAKE_GENERATION": "previous-week"}, GENERATION),
    ),
)
def test_resolver_rejects_mixed_or_partial_publications(
    override: dict[str, str],
    expected_generation: str,
    tmp_path: pathlib.Path,
    executable_tmp_path: pathlib.Path,
) -> None:
    _install_fake_podman(executable_tmp_path)
    environment = _fake_environment(tmp_path / "podman.log")
    environment.update(override)
    result = _run_helper(
        "resolve",
        expected_generation,
        "1",
        "1",
        str(tmp_path / "output.env"),
        "ghcr.io/kogeler/dkc-toolbox:latest",
        "ghcr.io/kogeler/dkc-kernel-build:latest",
        "ghcr.io/kogeler/dkc-apt-client:latest",
        path=executable_tmp_path,
        extra_environment=environment,
    )
    assert result.returncode != 0
    assert "no coherent public latest image bundle" in result.stderr


def test_image_workflow_has_one_read_only_pr_path_and_one_main_publisher() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "container-images.yml").read_text()
    )
    events = workflow.get("on", workflow.get(True))
    assert events["pull_request"]["branches"] == ["main"]
    assert events["pull_request"]["paths"] == events["push"]["paths"]
    jobs = workflow["jobs"]
    assert set(jobs) == {"verify-pull-request", "publish-main"}
    assert {job["runs-on"] for job in jobs.values()} == {"ubuntu-26.04"}
    verification = jobs["verify-pull-request"]
    publisher = jobs["publish-main"]
    assert verification["permissions"] == {"contents": "read"}
    assert "packages" not in verification["permissions"]
    assert "secrets." not in str(verification)
    assert "make container-images" in str(verification)
    assert "container-images-push" not in str(verification)
    assert publisher["permissions"] == {"contents": "read", "packages": "write"}
    assert publisher["timeout-minutes"] == 120
    assert "github.repository == 'kogeler/dkc-linux'" in publisher["if"]
    assert "github.ref == 'refs/heads/main'" in publisher["if"]
    assert "make container-images" in str(publisher)
    assert "make container-images-push" in str(publisher)
    commands = "\n".join(str(step.get("run", "")) for step in publisher["steps"])
    publication = next(step for step in publisher["steps"] if step.get("id") == "publish")
    publication_run = publication["run"]
    assert commands.index("make container-images") < commands.index("podman login ghcr.io")
    assert commands.count("make current-main") == 2
    assert publication_run.index("container-images-push") < publication_run.rindex(
        "podman logout ghcr.io"
    )
    assert publication_run.rindex("podman logout ghcr.io") < publication_run.index(
        "container-images-resolve"
    )
    assert "DKC_IMAGE_RESOLVE_TIMEOUT=3600" in publication_run
    assert "upload-artifact" not in str(publisher)


def test_main_ci_consumes_only_resolved_registry_images() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "ci.yml"
    workflow_text = workflow_path.read_text()
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]
    resolver = jobs["container_images"]
    assert "container-images-resolve" in str(resolver)
    assert "github.event.pull_request.base.sha" not in str(resolver)
    assert resolver["permissions"] == {"actions": "read", "contents": "read"}
    assert "podman build" not in workflow_text

    consumers = {
        "fast": ("TOOLBOX_IMAGE",),
        "release-preflight": ("BUILD_IMAGE",),
        "flavors": ("TOOLBOX_IMAGE", "BUILD_IMAGE"),
        "package-matrix": ("TOOLBOX_IMAGE", "APT_CLIENT_IMAGE"),
        "sign-repository": ("TOOLBOX_IMAGE",),
        "verify-repository": ("TOOLBOX_IMAGE", "APT_CLIENT_IMAGE"),
    }
    for name, image_variables in consumers.items():
        job = jobs[name]
        assert "container_images" in job["needs"]
        assert job["env"]["DKC_IMAGE_MODE"] == "registry"
        assert "DKC_IMAGE_BUNDLE_INPUT_SHA256" not in job["env"]
        assert "DKC_IMAGE_BUNDLE_GENERATION" not in job["env"]
        for variable in image_variables:
            assert "needs.container_images.outputs" in job["env"][variable]
