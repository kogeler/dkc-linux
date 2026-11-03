"""Read-only workflow-run status used as proof for a safe lease takeover."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .records import LeaseOwner

__all__ = ["workflow_attempt_terminal"]

_RESPONSE_LIMIT = 1_048_576


def workflow_attempt_terminal(token: str, owner: LeaseOwner) -> bool:
    """Return True only when GitHub proves the lease owner's attempt ended.

    Every failure to read or parse the run is treated as "not proven".
    """

    request = urllib.request.Request(
        f"https://api.github.com/repos/{owner.repository}/actions/runs/{owner.workflow_run_id}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}" if token else "",
            "User-Agent": "dkc-storage-lifecycle",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read(_RESPONSE_LIMIT + 1)
    except (OSError, urllib.error.URLError):
        return False
    if len(body) > _RESPONSE_LIMIT:
        return False
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(value, dict):
        return False
    repository = value.get("head_repository")
    return bool(
        isinstance(repository, dict)
        and repository.get("full_name") == owner.repository
        and value.get("status") == "completed"
        and value.get("run_attempt") == int(owner.run_attempt)
    )
