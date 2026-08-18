from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github/workflows"
REVIEWED_ACTIONS = {
    "actions/cache/restore@caa296126883cff596d87d8935842f9db880ef25",
    "actions/cache/save@caa296126883cff596d87d8935842f9db880ef25",
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
    "actions/download-artifact@70fc10c6e5e1ce46ad2ea6f2b72d43f7d47b13c3",
    "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f",
}


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(
                f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _workflow(path: Path) -> dict[str, Any]:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    assert isinstance(value, dict)
    return value


def _needs(job: dict[str, Any]) -> set[str]:
    value = job.get("needs", [])
    return {value} if isinstance(value, str) else set(value)


def test_every_workflow_mapping_has_unique_keys() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        _workflow(path)


def test_every_external_action_uses_an_exact_reviewed_revision() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        jobs = _workflow(path).get("jobs", {})
        for job_name, job in jobs.items():
            for step in job.get("steps", []):
                action = step.get("uses")
                if action is None or action.startswith("./"):
                    continue
                assert action in REVIEWED_ACTIONS, (
                    f"{path.name}:{job_name}/{step.get('name', '<unnamed>')} "
                    f"uses an unreviewed action revision: {action}"
                )


def test_ci_dag_references_only_declared_dependencies_and_is_acyclic() -> None:
    jobs = _workflow(WORKFLOWS / "ci.yml")["jobs"]
    assert isinstance(jobs, dict)
    for name, job in jobs.items():
        declared = _needs(job)
        assert declared <= set(jobs), f"{name} needs an unknown job"
        serialized = json.dumps(job)
        referenced = set(re.findall(r"needs\.([A-Za-z0-9_-]+)", serialized))
        assert referenced <= declared, f"{name} reads an undeclared dependency"
        for dependency, output in re.findall(
            r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)",
            serialized,
        ):
            assert output in jobs[dependency].get("outputs", {}), (
                f"{name} reads unknown output {dependency}.{output}"
            )
        step_ids = {
            step["id"] for step in job.get("steps", []) if "id" in step
        }
        referenced_steps = set(
            re.findall(r"steps\.([A-Za-z0-9_-]+)", serialized)
        )
        assert referenced_steps <= step_ids, f"{name} reads an unknown step output"

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            raise AssertionError(f"CI dependency cycle reaches {name}")
        if name in visited:
            return
        visiting.add(name)
        for dependency in _needs(jobs[name]):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)

    for name in jobs:
        visit(name)


def test_ci_artifacts_are_attempt_scoped_and_follow_the_dag() -> None:
    jobs = _workflow(WORKFLOWS / "ci.yml")["jobs"]
    producers: dict[str, set[str]] = {}
    consumers: list[tuple[str, str]] = []
    for job_name, job in jobs.items():
        download_paths: set[str] = set()
        for step in job.get("steps", []):
            action = step.get("uses", "")
            if action.startswith("actions/upload-artifact@"):
                name = step["with"]["name"]
                assert "${{ github.run_id }}-${{ github.run_attempt }}" in name
                assert "/latest" not in str(step["with"]["path"])
                producers.setdefault(name, set()).add(job_name)
            elif action.startswith("actions/download-artifact@"):
                name = step["with"]["name"]
                path = step["with"]["path"]
                assert path not in download_paths, f"{job_name} overlays two handoffs"
                download_paths.add(path)
                consumers.append((job_name, name))

    formatted_name = re.compile(
        r"^\$\{\{ format\('([^']+)', github\.run_id, github\.run_attempt\) \}\}$"
    )
    need_output = re.compile(
        r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)"
    )

    def resolve_names(value: str, seen: frozenset[tuple[str, str]] = frozenset()) -> set[str]:
        formatted = formatted_name.fullmatch(value)
        if formatted is not None:
            return {
                formatted.group(1)
                .replace("{0}", "${{ github.run_id }}")
                .replace("{1}", "${{ github.run_attempt }}")
            }
        references = need_output.findall(value)
        if not references:
            return {value}
        result: set[str] = set()
        for dependency, output in references:
            marker = (dependency, output)
            assert marker not in seen, "artifact-name outputs contain a cycle"
            result.update(
                resolve_names(
                    jobs[dependency]["outputs"][output],
                    seen | {marker},
                )
            )
        return result

    def ancestors(name: str) -> set[str]:
        result: set[str] = set()
        pending = list(_needs(jobs[name]))
        while pending:
            dependency = pending.pop()
            if dependency not in result:
                result.add(dependency)
                pending.extend(_needs(jobs[dependency]))
        return result

    for consumer, expression in consumers:
        artifacts = resolve_names(expression)
        assert artifacts, f"{consumer} has an empty artifact-name expression"
        for artifact in artifacts:
            assert artifact in producers, (
                f"{consumer} downloads an unknown same-run artifact"
            )
            assert producers[artifact] <= ancestors(consumer), (
                f"{consumer} can run without artifact producer {producers[artifact]}"
            )
        assert "github.run_attempt" not in expression, (
            f"{consumer} ignores the producer attempt during a partial rerun"
        )

    assert producers[
        "dkc-apt-unsigned-${{ github.run_id }}-${{ github.run_attempt }}"
    ] == {"package-matrix", "refresh-metadata"}
    assert all(len(value) == 1 for key, value in producers.items() if "apt-unsigned" not in key)


def test_ci_shell_is_only_thin_make_adapters() -> None:
    jobs = _workflow(WORKFLOWS / "ci.yml")["jobs"]
    for job_name, job in jobs.items():
        for step in job.get("steps", []):
            command = step.get("run")
            if command is not None:
                assert command.strip().startswith("make "), (
                    f"{job_name}/{step['name']} embeds workflow logic instead of a make target"
                )


def test_every_job_with_write_permission_is_canonical_main_only() -> None:
    jobs = _workflow(WORKFLOWS / "ci.yml")["jobs"]
    for name, job in jobs.items():
        permissions = job.get("permissions", {})
        if any(value == "write" for value in permissions.values()):
            condition = str(job.get("if", ""))
            assert "github.repository == 'kogeler/dkc-linux'" in condition, name
            assert "github.ref == 'refs/heads/main'" in condition, name
            assert "github.event_name != 'pull_request'" in condition, name
            assert "needs.fast.result == 'success'" in condition, name


def test_every_production_mutation_path_requires_the_fast_tier() -> None:
    jobs = _workflow(WORKFLOWS / "ci.yml")["jobs"]

    def ancestors(name: str) -> set[str]:
        result: set[str] = set()
        pending = list(_needs(jobs[name]))
        while pending:
            dependency = pending.pop()
            if dependency not in result:
                result.add(dependency)
                pending.extend(_needs(jobs[dependency]))
        return result

    for name in ("sign-repository", "publish-repository", "verify-published-state"):
        assert "fast" in ancestors(name), name


def test_ci_lifecycle_branches_converge_on_one_terminal_contract() -> None:
    jobs = _workflow(WORKFLOWS / "ci.yml")["jobs"]

    assert jobs["flavors"]["if"] == (
        "needs.lifecycle-decision.outputs.build_required == 'true'"
    )
    package_condition = str(jobs["package-matrix"]["if"])
    assert "build_required == 'true'" in package_condition
    assert "needs.flavors.result == 'success'" in package_condition
    assert "state_present == 'false'" in package_condition
    assert "needs.export-live-pool.result == 'success'" in package_condition

    refresh = jobs["refresh-metadata"]
    assert "maintenance_required == 'true'" in str(refresh["if"])
    assert "needs.export-live-pool.result == 'success'" in str(refresh["if"])
    assert "fast" in _needs(refresh)

    for name in ("current-main", "sign-repository"):
        condition = str(jobs[name]["if"])
        assert "needs.package-matrix.result == 'success'" in condition
        assert "needs.refresh-metadata.result == 'success'" in condition
    assert "lifecycle-decision" in _needs(jobs["sign-repository"])

    for name, successful_dependency in (
        ("publish-repository", "verify-repository"),
        ("verify-published-state", "publish-repository"),
    ):
        condition = str(jobs[name]["if"])
        assert condition.startswith("always() &&")
        assert f"needs.{successful_dependency}.result == 'success'" in condition

    terminal = jobs["lifecycle-result"]
    assert _needs(terminal) == {
        "fast",
        "lifecycle-decision",
        "verify-published-state",
    }
    cleanup = terminal["steps"][-1]
    for decision in ("build", "maintenance", "no_op"):
        assert f"decision == '{decision}'" in cleanup["if"]


def test_signing_consumes_the_typed_decision_before_using_the_key() -> None:
    jobs = _workflow(WORKFLOWS / "ci.yml")["jobs"]
    signing = jobs["sign-repository"]
    steps = {step["name"]: step for step in signing["steps"]}
    decision = steps["Download the lifecycle signing authorization"]
    assert decision["with"] == {
        "name": "${{ needs.lifecycle-decision.outputs.lifecycle_handoff }}",
        "path": "out/ci-lifecycle",
    }
    command = steps["Sign the strict repository request"]["run"]
    assert command.startswith("make github-apt-repository-sign ")
    assert "LIFECYCLE_DECISION_RESULT=out/ci-lifecycle" in command
