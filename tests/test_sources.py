"""Parsing the real Debian `Sources` index and selecting the newest source."""

from __future__ import annotations

import pathlib

import pytest

from dkc.debver import DebianVersion
from dkc.sources import MalformedIndex, parse_sources, select_newest

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "sources-linux-sid.txt"


@pytest.fixture(scope="module")
def real_index() -> str:
    if not FIXTURE.exists():
        pytest.skip(f"fixture missing; regenerate with: make fixtures ({FIXTURE})")
    # The capture header is our own provenance note. A real Sources index has no
    # comments, and the parser is strict about that on purpose, so the header is
    # stripped here rather than tolerated in the parser.
    return "\n".join(
        line for line in FIXTURE.read_text().splitlines() if not line.startswith("#")
    )


def test_real_index_has_many_versions(real_index: str) -> None:
    """The premise of the whole selection problem."""
    stanzas = parse_sources(real_index, "linux")
    assert len(stanzas) > 1, "an index with one version cannot test selection"


def test_selects_newest_from_real_index(real_index: str) -> None:
    stanzas = parse_sources(real_index, "linux")
    chosen = select_newest(stanzas)

    assert chosen.version != stanzas[0].version, (
        "the newest stanza must not be the first one, or this fixture no longer "
        "guards against first-stanza selection"
    )
    highest_by_comparison = max(s.version for s in stanzas)
    assert chosen.version == highest_by_comparison

    # Selection must be by Debian comparison, not by string order. On this
    # index the two happen to agree, which is precisely why the comparison is
    # spelled out rather than assumed.
    assert chosen.version >= DebianVersion.parse(max(str(s.version) for s in stanzas))


def test_chosen_stanza_carries_a_verifiable_inventory(real_index: str) -> None:
    chosen = select_newest(parse_sources(real_index, "linux"))

    dsc = chosen.dsc
    assert dsc.name.endswith(".dsc")
    assert len(dsc.sha256) == 64
    assert dsc.size > 0

    # Every member must be hash-anchored: a build input without a checksum is
    # not a build input.
    for member in chosen.files:
        assert len(member.sha256) == 64
        assert member.size > 0

    names = {m.name for m in chosen.files}
    assert any(n.endswith(".orig.tar.xz") for n in names), names
    assert any(n.endswith(".debian.tar.xz") for n in names), names


def test_uri_construction(real_index: str) -> None:
    chosen = select_newest(parse_sources(real_index, "linux"))
    uri = chosen.uri("http://deb.debian.org/debian/", chosen.dsc)
    assert uri.startswith("http://deb.debian.org/debian/pool/")
    assert uri.endswith(chosen.dsc.name)
    assert "//pool" not in uri.removeprefix("http://")


def test_other_packages_are_ignored(real_index: str) -> None:
    assert parse_sources(real_index, "definitely-not-a-package") == []


# --------------------------------------------------------------------------
# Rejection cases: a malformed index must fail closed, never be half-parsed.
# --------------------------------------------------------------------------

_GOOD = """\
Package: demo
Version: 1.0-1
Directory: pool/main/d/demo
Checksums-Sha256:
 {h} 100 demo_1.0-1.dsc
 {h} 200 demo_1.0.orig.tar.xz

""".format(h="a" * 64)


def test_minimal_valid_stanza() -> None:
    stanzas = parse_sources(_GOOD, "demo")
    assert len(stanzas) == 1
    assert str(stanzas[0].version) == "1.0-1"
    assert len(stanzas[0].files) == 2


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda s: s.replace("Directory: pool/main/d/demo", "Directory: /etc"),
         "absolute Directory"),
        (lambda s: s.replace("Directory: pool/main/d/demo", "Directory: pool/../../etc"),
         "path traversal in Directory"),
        (lambda s: s.replace("Directory: pool/main/d/demo", "Directory: pool//demo"),
         "non-normalized Directory"),
        (lambda s: s.replace("demo_1.0-1.dsc", "../../etc/passwd"),
         "path traversal in a member name"),
        (lambda s: s.replace("a" * 64, "a" * 63),
         "truncated SHA-256"),
        (lambda s: s.replace(" 100 ", " 0 "),
         "zero size"),
        (lambda s: s.replace(" 100 ", " notanumber "),
         "non-integer size"),
        (lambda s: s.replace("Checksums-Sha256:\n", "Checksums-Sha256:\n \n"),
         "malformed checksum line"),
        (lambda s: s.replace("Version: 1.0-1\n", ""),
         "missing Version"),
        (lambda s: s.replace("Directory: pool/main/d/demo\n", ""),
         "missing Directory"),
        (lambda s: s.replace("Version: 1.0-1\n", "Version: 1.0-1\nVersion: 2.0-1\n"),
         "duplicate field"),
    ],
)
def test_rejects_malformed_index(mutation, reason: str) -> None:
    with pytest.raises(ValueError):
        parse_sources(mutation(_GOOD), "demo")


def test_duplicate_member_is_rejected() -> None:
    duplicated = _GOOD.replace(
        f" {'a' * 64} 200 demo_1.0.orig.tar.xz",
        f" {'a' * 64} 200 demo_1.0-1.dsc",
    )
    with pytest.raises(MalformedIndex):
        parse_sources(duplicated, "demo")


def test_exactly_one_dsc_is_required() -> None:
    without = _GOOD.replace(f" {'a' * 64} 100 demo_1.0-1.dsc\n", "")
    with pytest.raises(MalformedIndex, match="exactly one"):
        parse_sources(without, "demo")

    two = _GOOD.replace(
        f" {'a' * 64} 200 demo_1.0.orig.tar.xz",
        f" {'a' * 64} 200 demo_1.0-2.dsc",
    )
    with pytest.raises(MalformedIndex, match="exactly one"):
        parse_sources(two, "demo")


def test_continuation_without_field_is_rejected() -> None:
    with pytest.raises(MalformedIndex):
        parse_sources(" orphaned continuation\n", "demo")


def test_select_newest_rejects_empty() -> None:
    with pytest.raises(MalformedIndex):
        select_newest([])
