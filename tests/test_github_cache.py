from __future__ import annotations

import json
import urllib.parse

import pytest

from dkc.github_cache import delete_release_caches, delete_run_image_artifacts


KEY_V2 = "dkc-release-v2-v2-" + "a" * 64
KEY_V3 = "dkc-release-v2-v3-" + "b" * 64


def test_exact_main_cache_cleanup_is_idempotent() -> None:
    entries = {
        KEY_V2: [{"id": 10, "key": KEY_V2, "ref": "refs/heads/main"}],
        KEY_V3: [{"id": 11, "key": KEY_V3, "ref": "refs/heads/main"}],
    }
    seen_authorization: list[str] = []

    def transport(method: str, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        seen_authorization.append(headers["Authorization"])
        if method == "GET":
            key = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["key"][0]
            return 200, json.dumps({"actions_caches": entries[key]}).encode()
        identifier = int(url.rsplit("/", 1)[1])
        for key in entries:
            entries[key] = [entry for entry in entries[key] if entry["id"] != identifier]
        return 204, b""

    assert (
        delete_release_caches(
            repository="owner/repository",
            selected_ref="refs/heads/main",
            keys=(KEY_V2, KEY_V3),
            token="secret-token",
            transport=transport,
        )
        == 2
    )
    assert (
        delete_release_caches(
            repository="owner/repository",
            selected_ref="refs/heads/main",
            keys=(KEY_V2, KEY_V3),
            token="secret-token",
            transport=transport,
        )
        == 0
    )
    assert set(seen_authorization) == {"Bearer secret-token"}


def test_cache_cleanup_rejects_other_refs_and_unexpected_records() -> None:
    with pytest.raises(ValueError, match="only be deleted from main"):
        delete_release_caches(
            repository="owner/repository",
            selected_ref="refs/heads/topic",
            keys=(KEY_V2,),
            token="token",
        )

    def wrong_record(
        method: str, url: str, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        del method, url, headers
        return 200, json.dumps(
            {
                "actions_caches": [
                    {"id": 12, "key": KEY_V2, "ref": "refs/heads/other"}
                ]
            }
        ).encode()

    with pytest.raises(RuntimeError, match="unexpected cache record"):
        delete_release_caches(
            repository="owner/repository",
            selected_ref="refs/heads/main",
            keys=(KEY_V2,),
            token="token",
            transport=wrong_record,
        )



def test_run_image_cleanup_deletes_only_this_runs_image_archives() -> None:
    artifacts = {
        11: "dkc-images-7-1-toolbox",
        12: "dkc-images-7-2-kernel-build",
        13: "dkc-source-7-1",
        14: "dkc-images-8-1-toolbox",
        15: "dkc-images-7-1-toolbox-extra",
    }
    deleted: list[int] = []

    def transport(method: str, url: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        parsed = urllib.parse.urlparse(url)
        if method == "GET":
            assert parsed.path == "/repos/kogeler/dkc-linux/actions/runs/7/artifacts"
            entries = [{"id": i, "name": n} for i, n in artifacts.items()]
            return 200, json.dumps({"artifacts": entries}).encode()
        identifier = int(parsed.path.rsplit("/", 1)[1])
        deleted.append(identifier)
        del artifacts[identifier]
        return 204, b""

    assert delete_run_image_artifacts(
        repository="kogeler/dkc-linux", run_id="7", token="t", transport=transport
    ) == 2
    assert sorted(deleted) == [11, 12]
    assert delete_run_image_artifacts(
        repository="kogeler/dkc-linux", run_id="7", token="t", transport=transport
    ) == 0


def test_run_image_cleanup_rejects_a_malformed_record() -> None:
    def transport(method: str, url: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
        return 200, json.dumps({"artifacts": [{"id": "x", "name": "dkc-images-7-1-toolbox"}]}).encode()

    with pytest.raises(RuntimeError, match="unexpected artifact record"):
        delete_run_image_artifacts(
            repository="kogeler/dkc-linux", run_id="7", token="t", transport=transport
        )
