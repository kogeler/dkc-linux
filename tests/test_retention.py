from __future__ import annotations

import pytest

from dkc.debver import DebianVersion
from dkc.retention import PoolObject, select_retained_objects


def item(key: str, version: str, size: int) -> PoolObject:
    return PoolObject(key, DebianVersion.parse(version), size)


def test_series_mode_keeps_every_patch_from_the_newest_three_series() -> None:
    objects = [
        item("7.2.1.deb", "7.2.1-1", 10),
        item("7.2.0.deb", "7.2.0-2", 10),
        item("7.1.9.deb", "7.1.9-1", 10),
        item("6.18.4.deb", "6.18.4-1", 10),
        item("6.12.9.deb", "6.12.9-1", 10),
    ]
    decision = select_retained_objects(objects, mode="series", max_bytes=None)
    assert decision.retained_series == ((7, 2), (7, 1), (6, 18))
    assert decision.retained_keys == frozenset(
        {"7.2.1.deb", "7.2.0.deb", "7.1.9.deb", "6.18.4.deb"}
    )
    assert decision.retained_bytes == 40


def test_size_mode_removes_complete_oldest_patch_releases() -> None:
    objects = [
        item("7.2.1-image.deb", "7.2.1-1", 20),
        item("7.2.1-source.dsc", "7.2.1-1", 5),
        item("7.2.0-image.deb", "7.2.0-2", 20),
        item("7.2.0-source.dsc", "7.2.0-2", 5),
        item("7.1.8.deb", "7.1.8-1", 25),
        item("7.1.7.deb", "7.1.7-1", 25),
        item("6.18.4.deb", "6.18.4-1", 25),
    ]
    decision = select_retained_objects(
        objects,
        mode="series-size",
        max_bytes=85,
        fixed_bytes=10,
    )
    assert decision.retained_keys == frozenset(
        {"7.2.1-image.deb", "7.2.1-source.dsc", "7.1.8.deb", "6.18.4.deb"}
    )
    assert decision.retained_bytes == 85
    assert decision.retained_series == ((7, 2), (7, 1), (6, 18))


def test_size_mode_never_removes_the_last_patch_of_a_series() -> None:
    objects = [
        item("7.2.1.deb", "7.2.1-1", 40),
        item("7.1.8.deb", "7.1.8-1", 40),
        item("6.18.4.deb", "6.18.4-1", 40),
    ]
    with pytest.raises(ValueError, match="newest patch release"):
        select_retained_objects(
            objects,
            mode="series-size",
            max_bytes=119,
        )


def test_one_patch_release_is_atomic_across_debian_revisions_and_source() -> None:
    objects = [
        item("new.deb", "7.1.8-1+dkc13.1", 50),
        item("old-r1.deb", "7.1.7-1+dkc13.1", 20),
        item("old-revision-2.deb", "7.1.7-2+dkc13.2", 20),
        item("old.orig.tar.xz", "7.1.7", 20),
    ]
    decision = select_retained_objects(
        objects,
        mode="series-size",
        max_bytes=50,
    )
    assert decision.retained_keys == frozenset({"new.deb"})


def test_fixed_non_kernel_bytes_participate_in_the_limit() -> None:
    objects = [item("new.deb", "7.1.8-1", 10)]
    with pytest.raises(ValueError, match="newest patch release"):
        select_retained_objects(
            objects,
            mode="series-size",
            max_bytes=99,
            fixed_bytes=90,
        )


@pytest.mark.parametrize(
    ("mode", "maximum"),
    (("series", 10), ("series-size", None), ("series-size", 0), ("other", None)),
)
def test_retention_mode_and_limit_are_strict(mode: str, maximum: int | None) -> None:
    with pytest.raises(ValueError):
        select_retained_objects(
            [item("new.deb", "7.1.8-1", 1)],
            mode=mode,  # type: ignore[arg-type]
            max_bytes=maximum,
        )
