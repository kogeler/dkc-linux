from __future__ import annotations

from dataclasses import dataclass

import pytest

from dkc.s3 import ListedObject
from dkc.storage_budget import project_storage


@dataclass(frozen=True)
class Write:
    relative_key: str
    size: int


def test_projection_replaces_mutable_adds_immutable_and_removes_retired() -> None:
    result = project_storage(
        [ListedObject("mutable", 10), ListedObject("retired", 20)],
        [Write("mutable", 12), Write("new", 30)],
        ["retired"],
    )
    assert result.object_count == 2
    assert result.size == 42


def test_projection_rejects_ambiguous_inventories() -> None:
    with pytest.raises(ValueError, match="both write and delete"):
        project_storage([], [Write("same", 1)], ["same"])
    with pytest.raises(ValueError, match="repeats"):
        project_storage([], [], ["same", "same"])
