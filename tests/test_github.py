from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from dkc.github import (
    authorize_lifecycle,
    export_lifecycle_outputs,
    export_image_bundle,
    export_source_environment,
    prepare_pull_request_qualification,
    require_terminal_result,
    write_run_identity,
    write_workflow_assignments,
)
from dkc.release_gate import load_discovery_decision
from dkc.source_discovery import build_inventory, make_variables
from dkc.serialize import boolean_text, parse_boolean_text


def _authorize(**overrides: str) -> bool:
    values = {
        "event": "schedule",
        "repository": "owner/repository",
        "selected_ref": "refs/heads/main",
        "canonical_repository": "owner/repository",
        "confirm_lifecycle": "false",
        "allow_empty_bootstrap": "false",
    }
    values.update(overrides)
    return authorize_lifecycle(**values)


def test_lifecycle_authorization_is_exact_and_bootstrap_is_manual() -> None:
    assert _authorize() is False
    with pytest.raises(ValueError, match="cannot authorize"):
        _authorize(event="push")
    assert _authorize(
        event="workflow_dispatch",
        confirm_lifecycle="true",
        allow_empty_bootstrap="true",
    ) is True
    with pytest.raises(ValueError, match="canonical main"):
        _authorize(selected_ref="refs/heads/topic")


def test_workflow_boolean_handoff_has_one_exact_round_trip() -> None:
    assert boolean_text(True) == "true"
    assert boolean_text(False) == "false"
    assert parse_boolean_text("true") is True
    assert parse_boolean_text("false") is False
    for invalid in ("", "0", "1", "True", "FALSE"):
        with pytest.raises(ValueError, match="exactly true or false"):
            parse_boolean_text(invalid)


def test_workflow_assignment_writes_are_idempotent_and_conflict_closed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "command"
    write_workflow_assignments(path, {"value": "one", "second": "two"})
    initial = path.read_bytes()
    write_workflow_assignments(path, {"second": "two", "value": "one"})
    assert path.read_bytes() == initial
    with pytest.raises(ValueError, match="another value"):
        write_workflow_assignments(path, {"value": "changed"})
    with pytest.raises(ValueError, match="unsafe value"):
        write_workflow_assignments(path, {"new": "line\nbreak"})


def test_run_identity_is_stable_for_one_job_and_rejects_another_role(
    tmp_path: Path,
) -> None:
    path = tmp_path / "environment"
    now = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)
    first = write_run_identity(
        environment_file=path,
        repository="owner/repository",
        workflow_run_id="123",
        run_attempt="2",
        role="package-matrix",
        now=now,
    )
    second = write_run_identity(
        environment_file=path,
        repository="owner/repository",
        workflow_run_id="123",
        run_attempt="2",
        role="package-matrix",
        now=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    assert first == second
    assert path.read_text(encoding="utf-8") == f"DKC_RUN_ID={first}\n"
    with pytest.raises(ValueError, match="conflicts"):
        write_run_identity(
            environment_file=path,
            repository="owner/repository",
            workflow_run_id="123",
            run_attempt="2",
            role="state-read",
            now=now,
        )


def test_image_bundle_export_is_typed_immutable_and_idempotent(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    output = tmp_path / "output"
    digest = "a" * 64
    values = {
        "apt_client_image": f"ghcr.io/kogeler/dkc-apt-client@sha256:{digest}",
        "build_image": f"ghcr.io/kogeler/dkc-kernel-build@sha256:{digest}",
        "bundle_generation": "20260817-abcdef12",
        "bundle_input_sha256": "b" * 64,
        "toolbox_image": f"ghcr.io/kogeler/dkc-toolbox@sha256:{digest}",
    }
    bundle.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(values.items())),
        encoding="utf-8",
    )
    export_image_bundle(bundle, output)
    initial = output.read_bytes()
    export_image_bundle(bundle, output)
    assert output.read_bytes() == initial
    bundle.write_text(bundle.read_text().replace("@sha256:", ":latest="), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable canonical digest"):
        export_image_bundle(bundle, tmp_path / "bad-output")


def test_source_environment_is_reconstructed_from_the_typed_handoff(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures/sources-linux-sid.txt"
    inventory = build_inventory(
        fixture.read_text(encoding="utf-8"),
        mirror="http://deb.debian.org/debian",
        discovered=datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc),
    )
    variables = make_variables(inventory)
    root = tmp_path / "source"
    root.mkdir()
    files = {
        "result.env": "status=PASS\nsource_discovery=PASS\n",
        "source-inventory.json": json.dumps(
            inventory, sort_keys=True, separators=(",", ":")
        )
        + "\n",
        "source.env": "".join(
            f"{key}={value}\n" for key, value in sorted(variables.items())
        ),
    }
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")
    (root / "evidence.sha256").write_text(
        "".join(
            f"{hashlib.sha256(body.encode()).hexdigest()}  {name}\n"
            for name, body in sorted(files.items())
        ),
        encoding="utf-8",
    )
    environment = tmp_path / "environment"
    export_source_environment(root, environment)
    initial = environment.read_bytes()
    export_source_environment(root, environment)
    assert environment.read_bytes() == initial

    decision_root = tmp_path / "qualification"
    repository_root = Path(__file__).parents[1]
    prepare_pull_request_qualification(
        root,
        decision_root,
        repository_root=repository_root,
        epoch=1_787_875_200,
        dkc_revision=1,
        lto_mode="thin",
        retention_mode="series-size",
        retention_max_bytes=9_500_000_000,
    )
    decision = load_discovery_decision(decision_root)
    assert decision.decision == "qualification"
    assert decision.build_required
    assert not decision.publish_allowed
    assert not decision.authoritative_state_read

    pull_request_output = tmp_path / "pull-request-output"
    export_lifecycle_outputs(
        decision_root,
        pull_request_output,
        repository_root=repository_root,
        event="pull_request",
        workflow_run_id="123",
        run_attempt="2",
    )
    pull_request_values = dict(
        line.split("=", 1)
        for line in pull_request_output.read_text(encoding="utf-8").splitlines()
    )
    assert pull_request_values["v2_cache_key"].startswith("dkc-release-v2-v2-")
    assert pull_request_values["v2_cache_transport_key"].startswith(
        "dkc-pr-123-2-v2-"
    )
    assert (
        pull_request_values["v2_cache_transport_key"]
        != pull_request_values["v2_cache_key"]
    )

    production_output = tmp_path / "production-output"
    export_lifecycle_outputs(
        decision_root,
        production_output,
        repository_root=repository_root,
        event="schedule",
        workflow_run_id="123",
        run_attempt="2",
    )
    production_values = dict(
        line.split("=", 1)
        for line in production_output.read_text(encoding="utf-8").splitlines()
    )
    assert production_values["v2_cache_transport_key"] == production_values[
        "v2_cache_key"
    ]

    (root / "source.env").write_text("DKC_SOURCE_VERSION=wrong\n", encoding="utf-8")
    with pytest.raises(ValueError):
        export_source_environment(root, tmp_path / "bad-environment")


@pytest.mark.parametrize(
    ("decision", "final_result"),
    (("no_op", "skipped"), ("build", "success"), ("maintenance", "success")),
)
def test_terminal_result_accepts_only_the_selected_path(
    decision: str, final_result: str
) -> None:
    require_terminal_result(
        decision=decision,
        decision_result="success",
        final_result=final_result,
    )
    with pytest.raises(ValueError, match="terminal state"):
        require_terminal_result(
            decision=decision,
            decision_result="success",
            final_result="failure",
        )
