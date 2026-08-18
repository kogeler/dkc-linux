"""Debian version parsing, comparison, and validation.

Why this exists rather than shelling out to dpkg everywhere: version ordering
decides which upstream source is built and which published artifact is newer, so
it runs in contexts where dpkg is not necessarily installed, and it must be
testable without a Debian userland. `dpkg` remains the reference implementation
and the test suite cross-checks every fixture against it when it is available.

The algorithm is Debian Policy 5.6.12: an optional numeric epoch, an upstream
version, and an optional Debian revision, each compared by alternating numeric
and non-numeric runs, where `~` sorts before everything including the empty
string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import total_ordering

__all__ = ["DebianVersion", "InvalidVersion", "compare"]


class InvalidVersion(ValueError):
    """A version string is not a valid Debian version."""


# Policy: upstream_version may contain alphanumerics and . + - : ~ and must
# start with a digit. The revision may contain alphanumerics and . + ~.
_UPSTREAM_RE = re.compile(r"^[0-9][A-Za-z0-9.+\-:~]*$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9.+~]*$")


def _order(char: str) -> int:
    """Collation weight for one character of a non-numeric run.

    `~` sorts before the empty string, digits are handled by the caller and
    never reach here, letters sort before every other punctuation character.
    """
    if char == "~":
        return -1
    if char.isdigit():
        return 0
    if char.isalpha():
        return ord(char)
    return ord(char) + 256


def _compare_fragment(left: str, right: str) -> int:
    """Compare one upstream or revision fragment."""
    i = j = 0
    while i < len(left) or j < len(right):
        # Non-numeric run, compared by the collation weights above. A run that
        # has ended on one side compares as weight 0, which is how `~` (weight
        # -1) sorts before the empty string.
        while (i < len(left) and not left[i].isdigit()) or (
            j < len(right) and not right[j].isdigit()
        ):
            left_weight = _order(left[i]) if i < len(left) and not left[i].isdigit() else 0
            right_weight = _order(right[j]) if j < len(right) and not right[j].isdigit() else 0
            if left_weight != right_weight:
                return -1 if left_weight < right_weight else 1
            i += 1
            j += 1

        # Numeric run, compared as integers so 10 sorts after 9. Leading zeroes
        # are insignificant, which is why this is not a string comparison.
        start_i, start_j = i, j
        while i < len(left) and left[i].isdigit():
            i += 1
        while j < len(right) and right[j].isdigit():
            j += 1
        left_num = int(left[start_i:i] or "0")
        right_num = int(right[start_j:j] or "0")
        if left_num != right_num:
            return -1 if left_num < right_num else 1
    return 0


def _normalized_fragment(value: str) -> tuple[tuple[str, str | int], ...]:
    """Return a hash key for dpkg's fragment equivalence classes.

    Leading zeroes are insignificant, and a final all-zero numeric run compares
    equal to no run (this is why an absent Debian revision equals revision
    ``0``).  A zero run between text is still a boundary: ``a0b`` sorts before
    ``ab`` and must not be normalized to the same key.
    """
    result: list[tuple[str, str | int]] = []
    index = 0
    while index < len(value):
        end = index
        if value[index].isdigit():
            while end < len(value) and value[end].isdigit():
                end += 1
            result.append(("n", int(value[index:end])))
        else:
            while end < len(value) and not value[end].isdigit():
                end += 1
            result.append(("s", value[index:end]))
        index = end
    if result and result[-1] == ("n", 0):
        result.pop()
    return tuple(result)


@total_ordering
@dataclass(frozen=True)
class DebianVersion:
    """A parsed, validated Debian version."""

    epoch: int
    upstream: str
    revision: str
    raw: str

    @classmethod
    def parse(cls, text: str) -> DebianVersion:
        if not text or text.strip() != text:
            raise InvalidVersion(f"empty or padded version: {text!r}")
        if any(c.isspace() for c in text):
            raise InvalidVersion(f"whitespace in version: {text!r}")

        rest = text
        epoch = 0
        if ":" in rest:
            head, _, tail = rest.partition(":")
            if not head.isdigit():
                raise InvalidVersion(f"non-numeric epoch in {text!r}")
            epoch = int(head)
            rest = tail

        # The Debian revision is everything after the *last* hyphen; an upstream
        # version may itself contain hyphens.
        if "-" in rest:
            upstream, _, revision = rest.rpartition("-")
        else:
            upstream, revision = rest, ""

        if not _UPSTREAM_RE.match(upstream):
            raise InvalidVersion(f"invalid upstream version in {text!r}")
        if not _REVISION_RE.match(revision):
            raise InvalidVersion(f"invalid Debian revision in {text!r}")
        if ":" in upstream and epoch == 0:
            raise InvalidVersion(f"colon in upstream version without an epoch: {text!r}")

        return cls(epoch=epoch, upstream=upstream, revision=revision, raw=text)

    @property
    def upstream_release(self) -> str:
        """The upstream release with any Debian-only suffix removed.

        For `7.1.7-1` this is `7.1.7`. Used to derive the kernel `X.Y` series
        and the ABI name, never for ordering.
        """
        return self.upstream

    @property
    def series(self) -> tuple[int, int]:
        """The upstream `X.Y` series, parsed rather than sliced from the string.

        Retention groups publications by this value, so a lexical filename sort
        must never be substituted for it.
        """
        match = re.match(r"^(\d+)(?:\.(\d+))?", self.upstream)
        if not match:
            raise InvalidVersion(f"cannot parse an X.Y series from {self.raw!r}")
        return int(match.group(1)), int(match.group(2) or 0)

    def __str__(self) -> str:
        return self.raw

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, DebianVersion):
            return NotImplemented
        return compare(self, other) < 0

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, DebianVersion):
            return NotImplemented
        return compare(self, other) == 0

    def __hash__(self) -> int:
        # Equality follows dpkg comparison, not spelling.  In particular,
        # 1.0, 1.0-0, 1.00 and 1.0-00 compare equal and therefore must hash
        # equally when used as dictionary keys or set members.
        return hash(
            (
                self.epoch,
                _normalized_fragment(self.upstream),
                _normalized_fragment(self.revision),
            )
        )


def compare(left: DebianVersion | str, right: DebianVersion | str) -> int:
    """Return -1, 0, or 1, matching `dpkg --compare-versions`."""
    a = DebianVersion.parse(left) if isinstance(left, str) else left
    b = DebianVersion.parse(right) if isinstance(right, str) else right

    if a.epoch != b.epoch:
        return -1 if a.epoch < b.epoch else 1
    result = _compare_fragment(a.upstream, b.upstream)
    if result:
        return result
    return _compare_fragment(a.revision, b.revision)
