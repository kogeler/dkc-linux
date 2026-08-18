"""Deterministic package-pool retention decisions.

The selector operates on typed object identities rather than filenames.  One
upstream patch release is indivisible: all of its binary packages and source
members enter or leave the live pool together.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Literal

from .debver import DebianVersion

__all__ = [
    "PoolObject",
    "RetentionDecision",
    "RetentionMode",
    "select_retained_objects",
]


RetentionMode = Literal["series", "series-size"]


@dataclass(frozen=True)
class PoolObject:
    key: str
    version: DebianVersion
    size: int

    def __post_init__(self) -> None:
        if not self.key or self.key.startswith("/") or self.key.endswith("/"):
            raise ValueError("retention object key is unsafe")
        if self.size < 0:
            raise ValueError("retention object size must not be negative")

    @property
    def series(self) -> tuple[int, int]:
        return self.version.series

    @property
    def patch_release(self) -> tuple[int, str]:
        return self.version.epoch, self.version.upstream_release


@dataclass(frozen=True)
class RetentionDecision:
    mode: RetentionMode
    retained_keys: frozenset[str]
    retained_series: tuple[tuple[int, int], ...]
    retained_patch_releases: tuple[tuple[int, str], ...]
    retained_bytes: int
    fixed_bytes: int
    max_bytes: int | None

    def __post_init__(self) -> None:
        if self.retained_bytes < self.fixed_bytes or self.fixed_bytes < 0:
            raise ValueError("retention byte accounting is inconsistent")
        if self.mode == "series":
            if self.max_bytes is not None:
                raise ValueError("series retention must not carry a byte limit")
        elif self.mode == "series-size":
            if self.max_bytes is None or self.max_bytes < 1:
                raise ValueError("series-size retention requires a positive byte limit")
            if self.retained_bytes > self.max_bytes:
                raise ValueError("retention decision exceeds its byte limit")
        else:
            raise ValueError("unknown retention mode")


def _release_version(release: tuple[int, str]) -> DebianVersion:
    epoch, upstream = release
    raw = f"{epoch}:{upstream}" if epoch else upstream
    return DebianVersion(epoch=epoch, upstream=upstream, revision="", raw=raw)


def select_retained_objects(
    objects: Iterable[PoolObject],
    *,
    mode: RetentionMode,
    max_bytes: int | None,
    fixed_bytes: int = 0,
    max_series: int = 3,
) -> RetentionDecision:
    """Keep at most ``max_series`` and optionally enforce one byte budget.

    The newest patch release in every retained ``X.Y`` series is protected.
    When a byte limit applies, all other patch releases are candidates in
    Debian version order, oldest first.  The selector fails rather than remove
    the protected release of any retained series.
    """

    values = tuple(objects)
    if max_series < 1:
        raise ValueError("retention series limit must be positive")
    if fixed_bytes < 0:
        raise ValueError("fixed retention bytes must not be negative")
    if len({item.key for item in values}) != len(values):
        raise ValueError("retention inventory repeats an object key")
    if mode == "series":
        if max_bytes is not None:
            raise ValueError("series retention does not accept a byte limit")
    elif mode == "series-size":
        if max_bytes is None or max_bytes < 1:
            raise ValueError("series-size retention requires a positive byte limit")
    else:
        raise ValueError("unknown retention mode")
    if not values:
        raise ValueError("kernel package pool is empty")

    retained_series = tuple(
        sorted({item.series for item in values}, reverse=True)[:max_series]
    )
    retained_series_set = set(retained_series)
    selected = {item.key: item for item in values if item.series in retained_series_set}
    releases: dict[tuple[int, str], list[PoolObject]] = {}
    for item in selected.values():
        releases.setdefault(item.patch_release, []).append(item)

    releases_by_series: dict[tuple[int, int], list[tuple[int, str]]] = {}
    for release, members in releases.items():
        series = members[0].series
        if any(member.series != series for member in members):
            raise ValueError("one patch release spans multiple upstream series")
        releases_by_series.setdefault(series, []).append(release)
    protected = {
        max(series_releases, key=_release_version)
        for series_releases in releases_by_series.values()
    }

    total = fixed_bytes + sum(item.size for item in selected.values())
    if mode == "series-size" and max_bytes is not None and total > max_bytes:
        candidates = sorted(
            (release for release in releases if release not in protected),
            key=_release_version,
        )
        for release in candidates:
            if total <= max_bytes:
                break
            for item in releases[release]:
                selected.pop(item.key)
                total -= item.size
        if total > max_bytes:
            raise ValueError(
                "the newest patch release of every retained series exceeds the "
                "configured byte limit"
            )

    retained_release_set = {item.patch_release for item in selected.values()}
    for series in retained_series:
        if not any(item.series == series for item in selected.values()):
            raise AssertionError("retention removed the last patch release of a series")
    return RetentionDecision(
        mode=mode,
        retained_keys=frozenset(selected),
        retained_series=retained_series,
        retained_patch_releases=tuple(
            sorted(retained_release_set, key=_release_version, reverse=True)
        ),
        retained_bytes=total,
        fixed_bytes=fixed_bytes,
        max_bytes=max_bytes,
    )
