"""Pure policy and idempotent file adapters for GitHub workflow jobs."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

from .handoffs import load_source_handoff
from .release_gate import discovery_decision_outputs, load_discovery_decision
from .release_cache import release_cache_identity
from .source_discovery import make_variables

__all__ = [
    "authorize_lifecycle",
    "export_lifecycle_outputs",
    "export_image_bundle",
    "export_source_environment",
    "require_terminal_result",
    "write_workflow_assignments",
    "write_run_identity",
]


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_RUN_ID_RE = re.compile(r"^[1-9][0-9]{0,19}$")
_ATTEMPT_RE = re.compile(r"^[1-9][0-9]{0,9}$")
_DKC_RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GENERATION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _boolean(value: str, name: str, *, empty_is_false: bool = False) -> bool:
    if empty_is_false and value == "":
        return False
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be exactly true or false")
    return value == "true"


def _existing_assignments(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or not _OUTPUT_KEY_RE.fullmatch(key):
            continue
        if key in result and result[key] != value:
            raise ValueError(f"workflow command file contains conflicting {key}")
        result[key] = value
    return result


def write_workflow_assignments(path: Path, values: dict[str, str]) -> None:
    """Append GitHub command-file assignments exactly once.

    Repeating the same operation is a no-op. A different value for an already
    written key is a conflict rather than an implicit overwrite.
    """

    if not values or any(not _OUTPUT_KEY_RE.fullmatch(key) for key in values):
        raise ValueError("workflow output contains an unsafe key")
    if any("\n" in value or "\r" in value or "\x00" in value for value in values.values()):
        raise ValueError("workflow output contains an unsafe value")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _existing_assignments(path)
    pending: list[tuple[str, str]] = []
    for key, value in sorted(values.items()):
        if key in existing:
            if existing[key] != value:
                raise ValueError(f"workflow output {key} already has another value")
        else:
            pending.append((key, value))
    if not pending:
        return
    with path.open("a", encoding="utf-8") as stream:
        for key, value in pending:
            stream.write(f"{key}={value}\n")


def authorize_lifecycle(
    *,
    event: str,
    repository: str,
    selected_ref: str,
    canonical_repository: str,
    confirm_lifecycle: str,
    allow_empty_bootstrap: str,
) -> bool:
    if (
        not _REPOSITORY_RE.fullmatch(canonical_repository)
        or repository != canonical_repository
        or selected_ref != "refs/heads/main"
    ):
        raise ValueError("production lifecycle requires canonical main")
    if event == "schedule":
        return False
    if event == "workflow_dispatch":
        if not _boolean(confirm_lifecycle, "manual lifecycle confirmation"):
            raise ValueError("manual lifecycle was not confirmed")
        return _boolean(
            allow_empty_bootstrap,
            "manual bootstrap permission",
            empty_is_false=True,
        )
    raise ValueError("event cannot authorize the production lifecycle")


def write_run_identity(
    *,
    environment_file: Path,
    repository: str,
    workflow_run_id: str,
    run_attempt: str,
    role: str,
    now: datetime | None = None,
) -> str:
    if not _REPOSITORY_RE.fullmatch(repository):
        raise ValueError("workflow repository is unsafe")
    if not _RUN_ID_RE.fullmatch(workflow_run_id):
        raise ValueError("workflow run ID is invalid")
    if not _ATTEMPT_RE.fullmatch(run_attempt):
        raise ValueError("workflow run attempt is invalid")
    if not _ROLE_RE.fullmatch(role):
        raise ValueError("workflow role is unsafe")
    existing = _existing_assignments(environment_file).get("DKC_RUN_ID")
    identity = f"{repository}:{workflow_run_id}:{run_attempt}:{role}"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
    if existing is not None:
        if not _DKC_RUN_ID_RE.fullmatch(existing) or not existing.endswith(
            f"-{suffix}"
        ):
            raise ValueError("DKC_RUN_ID conflicts with this workflow job")
        return existing
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("workflow identity clock must be timezone-aware")
    value = f"{clock.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{suffix}"
    write_workflow_assignments(environment_file, {"DKC_RUN_ID": value})
    return value


def export_lifecycle_outputs(
    decision_root: Path,
    output_file: Path,
    *,
    repository_root: Path,
) -> None:
    decision = load_discovery_decision(decision_root)
    values = discovery_decision_outputs(decision)
    identities = {
        flavor: release_cache_identity(
            decision, flavor=flavor, repository_root=repository_root
        )
        for flavor in ("v2", "v3")
    }
    values.update(
        {
            "v2_cache_key": identities["v2"].key(),
            "v3_cache_key": identities["v3"].key(),
        }
    )
    write_workflow_assignments(output_file, values)


def export_source_environment(source_root: Path, environment_file: Path) -> None:
    inventory = load_source_handoff(source_root)
    values = make_variables(inventory)
    write_workflow_assignments(environment_file, values)


def export_image_bundle(bundle_file: Path, output_file: Path) -> None:
    expected_keys = {
        "apt_client_image",
        "build_image",
        "bundle_generation",
        "bundle_input_sha256",
        "toolbox_image",
    }
    values: dict[str, str] = {}
    for line in bundle_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected_keys or key in values or not value:
            raise ValueError("resolved image bundle output is malformed")
        values[key] = value
    if set(values) != expected_keys:
        raise ValueError("resolved image bundle output is incomplete")
    if not re.fullmatch(r"[0-9a-f]{64}", values["bundle_input_sha256"]):
        raise ValueError("resolved image bundle fingerprint is malformed")
    if not _GENERATION_RE.fullmatch(values["bundle_generation"]):
        raise ValueError("resolved image bundle generation is malformed")
    expected_repositories = {
        "toolbox_image": "ghcr.io/kogeler/dkc-toolbox",
        "build_image": "ghcr.io/kogeler/dkc-kernel-build",
        "apt_client_image": "ghcr.io/kogeler/dkc-apt-client",
    }
    for key, repository in expected_repositories.items():
        if not re.fullmatch(
            re.escape(repository) + r"@sha256:[0-9a-f]{64}", values[key]
        ):
            raise ValueError("resolved image reference is not an immutable canonical digest")
    write_workflow_assignments(output_file, values)


def require_terminal_result(
    *, decision: str, decision_result: str, final_result: str
) -> None:
    if decision_result != "success":
        raise ValueError("lifecycle decision job did not succeed")
    expected = {
        "no_op": "skipped",
        "build": "success",
        "maintenance": "success",
    }
    if decision not in expected:
        raise ValueError("lifecycle decision has no successful terminal state")
    if final_result != expected[decision]:
        raise ValueError("workflow did not reach the terminal state required by its decision")
