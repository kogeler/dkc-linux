"""Debian version comparison, cross-checked against dpkg itself.

The comparator decides which upstream source is built and which artifact is
newer, so "looks right" is not good enough: every ordering case here is also
verified against `dpkg --compare-versions` when dpkg is available, which it is
inside the toolbox container.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from dkc.debver import DebianVersion, InvalidVersion, compare

# Cases chosen to cover the parts of Policy 5.6.12 that are easy to get wrong.
ORDERING_CASES: list[tuple[str, str]] = [
    # Plain numeric ordering, where a string comparison would fail.
    ("7.0.9-1", "7.0.10-1"),
    ("6.12.94-1", "6.18.5-1"),
    ("7.1.6-1", "7.1.7-1"),
    # Debian revision breaks the tie.
    ("7.0.12-1", "7.0.12-2"),
    # Epoch dominates everything.
    ("7.1.7-1", "1:6.0.0-1"),
    ("1:1.0-1", "2:0.1-1"),
    # A tilde sorts before everything, including the empty string.
    ("1.0~rc1-1", "1.0-1"),
    ("1.0~~", "1.0~"),
    ("1.0~", "1.0"),
    ("1.0~rc1-1", "1.0~rc2-1"),
    # Letters sort before non-letter punctuation.
    ("1.0a-1", "1.0+b1-1"),
    # Leading zeroes are insignificant in numeric runs.
    ("1.007-1", "1.8-1"),
    # A missing revision is older than any revision.
    ("1.0", "1.0-1"),
    # DKC versions must sort above the Debian version they derive from, and
    # successive DKC revisions must order.
    ("7.1.7-1", "7.1.7-1+dkc13.1"),
    ("7.1.7-1+dkc13.1", "7.1.7-1+dkc13.2"),
    ("7.1.7-1+dkc13.2", "7.1.7-2"),
    ("7.1.7-1+dkc13.9", "7.1.7-1+dkc13.10"),
]

EQUAL_CASES: list[tuple[str, str]] = [
    ("1.0-1", "1.0-1"),
    ("0:1.0-1", "1.0-1"),
    ("1.0", "1.0"),
    ("1.0", "1.0-0"),
    ("1.00-00", "1.0"),
]


def _dpkg_available() -> bool:
    return shutil.which("dpkg") is not None


def _dpkg_compare(left: str, op: str, right: str) -> bool:
    return (
        subprocess.run(
            ["dpkg", "--compare-versions", left, op, right],
            check=False,
        ).returncode
        == 0
    )


@pytest.mark.parametrize(("lower", "higher"), ORDERING_CASES)
def test_ordering(lower: str, higher: str) -> None:
    assert compare(lower, higher) < 0, f"{lower} should sort below {higher}"
    assert compare(higher, lower) > 0
    assert DebianVersion.parse(lower) < DebianVersion.parse(higher)


@pytest.mark.parametrize(("left", "right"), EQUAL_CASES)
def test_equality(left: str, right: str) -> None:
    assert compare(left, right) == 0
    assert DebianVersion.parse(left) == DebianVersion.parse(right)


@pytest.mark.skipif(not _dpkg_available(), reason="dpkg is not installed")
@pytest.mark.parametrize(("lower", "higher"), ORDERING_CASES)
def test_matches_dpkg_ordering(lower: str, higher: str) -> None:
    """dpkg is the reference implementation; disagreement is our bug."""
    assert _dpkg_compare(lower, "lt", higher)
    assert not _dpkg_compare(higher, "lt", lower)


@pytest.mark.skipif(not _dpkg_available(), reason="dpkg is not installed")
@pytest.mark.parametrize(("left", "right"), EQUAL_CASES)
def test_matches_dpkg_equality(left: str, right: str) -> None:
    assert _dpkg_compare(left, "eq", right)


@pytest.mark.parametrize(("left", "right"), EQUAL_CASES)
def test_equal_versions_have_equal_hashes(left: str, right: str) -> None:
    assert hash(DebianVersion.parse(left)) == hash(DebianVersion.parse(right))
    assert len({DebianVersion.parse(left), DebianVersion.parse(right)}) == 1


@pytest.mark.parametrize(
    "bad",
    [
        "",
        " 1.0-1",
        "1.0-1 ",
        "1.0 -1",
        "-1.0",  # upstream must start with a digit
        "a1.0-1",
        "x:1.0-1",  # non-numeric epoch
        "1.0-1_bad",  # underscore is not allowed in a revision
        "1.0-1/etc",  # would be unsafe as a path component
        "1.0\n-1",
    ],
)
def test_rejects_malformed(bad: str) -> None:
    with pytest.raises(InvalidVersion):
        DebianVersion.parse(bad)


def test_lexical_ordering_is_wrong_in_general() -> None:
    """A string maximum agrees with Debian ordering only by coincidence.

    On the full sid list it happens to give the right answer, which is exactly
    why it is dangerous: the bug would sit unnoticed until a patch release
    crossed a digit boundary.
    """
    crossing_ten = ["7.0.9-1", "7.0.10-1"]
    assert max(crossing_ten) == "7.0.9-1"
    assert compare(*crossing_ten) < 0

    tilde = ["1.0~rc1-1", "1.0-1"]
    assert max(tilde) == "1.0~rc1-1"
    assert compare(*tilde) < 0

    epoch = ["1:1.0-1", "9.9-1"]
    assert max(epoch) == "9.9-1"
    assert compare(*epoch) > 0


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("7.1.7-1", (7, 1)),
        ("6.12.94-1", (6, 12)),
        ("1:7.0-2", (7, 0)),
        ("7.2~rc3-1", (7, 2)),
    ],
)
def test_series_is_parsed_not_sliced(version: str, expected: tuple[int, int]) -> None:
    assert DebianVersion.parse(version).series == expected


def test_series_ordering_is_numeric() -> None:
    """Retention keeps the newest three X.Y series, so 6.9 must not beat 6.12."""
    series = sorted(
        DebianVersion.parse(v).series for v in ["6.9-1", "6.12-1", "7.0-1", "6.18-1"]
    )
    assert series == [(6, 9), (6, 12), (6, 18), (7, 0)]
