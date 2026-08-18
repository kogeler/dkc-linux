"""Canonical serialization and the build-input digest."""

from __future__ import annotations

import pytest

from dkc.buildid import (
    BuildInputs,
    normalized_policy_config,
    policy_config_digest,
)
from dkc.serialize import dumps, sha256_of


def _inputs(**overrides: object) -> BuildInputs:
    base = dict(
        schema_version=1,
        debian_source_version="7.1.7-1",
        dsc_sha256="6b" + "0" * 62,
        source_member_sha256={
            "linux_7.1.7.orig.tar.xz": "cb" + "0" * 62,
            "linux_7.1.7-1.debian.tar.xz": "a8" + "0" * 62,
        },
        dkc_revision=1,
        overlay_sha256="ab" + "0" * 62,
        flavor_config_sha256={"v2": "02" + "0" * 62, "v3": "03" + "0" * 62, "v4": "04" + "0" * 62},
        flavor_policy={"v2": "x86-64-v2", "v3": "x86-64-v3", "v4": "x86-64-v4"},
        base_image_digest="docker.io/library/debian@sha256:" + "38" * 32,
        toolchain_lock_sha256="7c" + "0" * 62,
        build_policy_revision=1,
        lto_mode="none",
        dependency_lock={"clang-21:amd64": "21.1.8-1~bpo13+1@" + "42" * 32},
    )
    base.update(overrides)
    return BuildInputs(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Canonical serialization
# --------------------------------------------------------------------------


def test_key_order_does_not_change_the_bytes() -> None:
    assert dumps({"b": 1, "a": 2}) == dumps({"a": 2, "b": 1})


def test_output_is_stable_across_calls() -> None:
    value = {"z": [1, 2, {"y": "ü"}], "a": None, "m": True}
    assert dumps(value) == dumps(value)


def test_non_ascii_is_not_escaped() -> None:
    assert "ü" in dumps({"k": "ü"})


def test_output_ends_with_a_newline() -> None:
    assert dumps({"a": 1}).endswith("}\n")


def test_floats_are_rejected() -> None:
    """A float's repr is platform-sensitive; no signed record needs one."""
    with pytest.raises(TypeError, match="float"):
        dumps({"size": 1.0})
    with pytest.raises(TypeError, match="float"):
        dumps({"nested": {"list": [1, 2.5]}})


def test_non_string_keys_are_rejected() -> None:
    with pytest.raises(TypeError):
        dumps({1: "a"})


def test_hash_is_of_the_canonical_form() -> None:
    assert sha256_of({"a": 1, "b": 2}) == sha256_of({"b": 2, "a": 1})


# --------------------------------------------------------------------------
# Build-input digest
# --------------------------------------------------------------------------


def test_digest_is_deterministic() -> None:
    assert _inputs().digest() == _inputs().digest()


def test_build_id_is_lowercase_hex_of_the_required_length() -> None:
    build_id = _inputs().build_id()
    assert len(build_id) == 12
    assert build_id == build_id.lower()
    int(build_id, 16)


def test_build_id_must_not_be_shortened() -> None:
    with pytest.raises(ValueError):
        _inputs().build_id(length=8)


def test_policy_config_digest_ignores_only_derived_identity_fields() -> None:
    pre = {
        "CONFIG_LOCALVERSION": '"-v2-amd64"',
        "CONFIG_BUILD_SALT": '""',
        "CONFIG_RUST": "y",
        "CONFIG_MODULE_SIG": "n",
    }
    final = {
        **pre,
        "CONFIG_LOCALVERSION": '"-publication-v2-amd64"',
        "CONFIG_BUILD_SALT": '"publication-v2-amd64"',
    }
    assert policy_config_digest(pre) == policy_config_digest(final)
    assert "CONFIG_LOCALVERSION" not in normalized_policy_config(final)
    assert "CONFIG_BUILD_SALT" not in normalized_policy_config(final)
    assert normalized_policy_config(final)["CONFIG_RUST"] == "y"
    final["CONFIG_RUST"] = "n"
    assert policy_config_digest(pre) != policy_config_digest(final)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("debian_source_version", "7.1.8-1"),
        ("dkc_revision", 2),
        ("overlay_sha256", "ff" + "0" * 62),
        ("base_image_digest", "docker.io/library/debian@sha256:" + "ff" * 32),
        ("toolchain_lock_sha256", "ff" + "0" * 62),
        ("build_policy_revision", 2),
        ("lto_mode", "thin"),
        ("dsc_sha256", "ff" + "0" * 62),
    ],
)
def test_every_input_changes_the_identity(field: str, value: object) -> None:
    """If an input can change the bytes we ship, it must change the identity."""
    assert _inputs(**{field: value}).digest() != _inputs().digest()


def test_flavor_configuration_is_part_of_the_identity() -> None:
    changed = _inputs(
        flavor_config_sha256={"v2": "02" + "0" * 62, "v3": "ff" + "0" * 62, "v4": "04" + "0" * 62}
    )
    assert changed.digest() != _inputs().digest()


def test_source_members_are_part_of_the_identity() -> None:
    changed = _inputs(
        source_member_sha256={
            "linux_7.1.7.orig.tar.xz": "ff" + "0" * 62,
            "linux_7.1.7-1.debian.tar.xz": "a8" + "0" * 62,
        }
    )
    assert changed.digest() != _inputs().digest()


def test_flavor_order_is_fixed_not_dictionary_order() -> None:
    """The digest must not depend on how a caller happened to build the dict."""
    reordered = _inputs(
        flavor_config_sha256={"v4": "04" + "0" * 62, "v2": "02" + "0" * 62, "v3": "03" + "0" * 62}
    )
    assert reordered.digest() == _inputs().digest()


def test_missing_flavor_is_refused() -> None:
    with pytest.raises(ValueError, match="v4"):
        _inputs(flavor_config_sha256={"v2": "02" + "0" * 62, "v3": "03" + "0" * 62}).digest()


def test_missing_or_extra_flavor_policy_is_refused() -> None:
    with pytest.raises(ValueError, match="flavor policy"):
        _inputs(flavor_policy={"v2": "x86-64-v2", "v3": "x86-64-v3"}).digest()
    with pytest.raises(ValueError, match="unexpected"):
        _inputs(
            flavor_policy={
                "v2": "x86-64-v2",
                "v3": "x86-64-v3",
                "v4": "x86-64-v4",
                "v5": "future",
            }
        ).digest()


def test_malformed_digest_input_is_refused() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _inputs(dsc_sha256="not-a-digest").digest()


def test_incomplete_toolchain_identity_is_refused() -> None:
    with pytest.raises(ValueError, match="immutable registry"):
        _inputs(base_image_digest="sha256:deadbeef").digest()
    with pytest.raises(ValueError, match="must not be empty"):
        _inputs(dependency_lock={}).digest()
    with pytest.raises(ValueError, match="dependency record"):
        _inputs(dependency_lock={"clang-21": "unhashed"}).digest()


def test_unknown_lto_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="LTO mode"):
        _inputs(lto_mode="fast").digest()


def test_all_three_flavors_share_one_build_id() -> None:
    """One publication, one identity: the flavor suffix makes releases unique."""
    inputs = _inputs()
    assert inputs.build_id() == _inputs().build_id()
