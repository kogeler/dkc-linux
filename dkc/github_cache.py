"""Narrow GitHub Actions cache cleanup for published release candidates."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence

from .release_cache import RELEASE_CACHE_REVISION

__all__ = ["delete_release_caches"]


_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_KEY_RE = re.compile(
    rf"^dkc-release-v{RELEASE_CACHE_REVISION}-v[23]-[0-9a-f]{{64}}$"
)
_API_ROOT = "https://api.github.com"
Transport = Callable[[str, str, dict[str, str]], tuple[int, bytes]]


def _transport(method: str, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    request = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"GitHub cache API rejected a {method} request with HTTP {exc.code}"
        ) from None
    except (OSError, TimeoutError) as exc:
        raise RuntimeError(f"GitHub cache API {method} request failed") from exc


def _list_caches(
    repository: str,
    selected_ref: str,
    key: str,
    headers: dict[str, str],
    transport: Transport,
) -> tuple[int, ...]:
    owner, name = repository.split("/", 1)
    base = (
        f"{_API_ROOT}/repos/{urllib.parse.quote(owner, safe='')}"
        f"/{urllib.parse.quote(name, safe='')}/actions/caches"
    )
    result: list[int] = []
    page = 1
    while True:
        query = urllib.parse.urlencode(
            {"key": key, "ref": selected_ref, "per_page": 100, "page": page}
        )
        status, body = transport("GET", f"{base}?{query}", headers)
        if status != 200:
            raise RuntimeError("GitHub cache API returned an unexpected list status")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("GitHub cache API returned malformed list data") from exc
        entries = payload.get("actions_caches") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            raise RuntimeError("GitHub cache API returned an invalid cache list")
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or entry.get("key") != key
                or entry.get("ref") != selected_ref
                or not isinstance(entry.get("id"), int)
                or isinstance(entry.get("id"), bool)
                or entry["id"] < 1
            ):
                raise RuntimeError("GitHub cache API returned an unexpected cache record")
            result.append(entry["id"])
        if len(entries) < 100:
            break
        page += 1
        if page > 100:
            raise RuntimeError("GitHub cache API pagination exceeded its safety bound")
    if len(result) != len(set(result)):
        raise RuntimeError("GitHub cache API repeated a cache record")
    return tuple(result)


def delete_release_caches(
    *,
    repository: str,
    selected_ref: str,
    keys: Sequence[str],
    token: str,
    transport: Transport | None = None,
) -> int:
    """Delete only exact accepted-result keys from canonical ``main``.

    Listing and deleting by numeric cache ID avoids prefix matching at the
    destructive boundary.  Repeating the operation after a partial failure is
    safe: already absent entries are simply not listed.
    """

    if not _REPOSITORY_RE.fullmatch(repository):
        raise ValueError("GitHub repository is invalid")
    if selected_ref != "refs/heads/main":
        raise ValueError("release caches can only be deleted from main")
    if not token or any(character in token for character in "\r\n\x00"):
        raise ValueError("GitHub token is absent or malformed")
    normalized = tuple(keys)
    if not normalized or len(normalized) != len(set(normalized)):
        raise ValueError("release-cache key set is empty or duplicated")
    if any(not _KEY_RE.fullmatch(key) for key in normalized):
        raise ValueError("release-cache key is malformed")
    request = transport or _transport
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "dkc-linux-release-cache-cleanup",
    }
    owner, name = repository.split("/", 1)
    base = (
        f"{_API_ROOT}/repos/{urllib.parse.quote(owner, safe='')}"
        f"/{urllib.parse.quote(name, safe='')}/actions/caches"
    )
    removed = 0
    for key in normalized:
        identifiers = _list_caches(repository, selected_ref, key, headers, request)
        for identifier in identifiers:
            status, body = request("DELETE", f"{base}/{identifier}", headers)
            if status != 204 or body:
                raise RuntimeError("GitHub cache API returned an unexpected delete result")
            removed += 1
        if _list_caches(repository, selected_ref, key, headers, request):
            raise RuntimeError("GitHub cache deletion did not reach an empty exact key")
    return removed
