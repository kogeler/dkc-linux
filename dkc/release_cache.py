"""Content-addressed handoffs for accepted kernel flavor results.

The remote cache is only a transport.  A restored directory is accepted only
after its complete file inventory and the build, attestation, selftest, and VM
identities have been checked against the current lifecycle decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass
from typing import Any

from .evidence import verify_evidence_directory
from .records import DiscoveryDecision
from .release_gate import load_discovery_decision
from .serialize import dumps, sha256_of
from .validationpolicy import validation_policy_digest

__all__ = [
    "RELEASE_CACHE_REVISION",
    "ReleaseCacheIdentity",
    "prepare_release_cache",
    "release_cache_identity",
    "verify_release_cache",
]


RELEASE_CACHE_REVISION = 2
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BUILD_IMAGE_RE = re.compile(
    r"^ghcr\.io/kogeler/dkc-kernel-build@(sha256:[0-9a-f]{64})$"
)
_TOOLBOX_IMAGE_RE = re.compile(
    r"^ghcr\.io/kogeler/dkc-toolbox@sha256:[0-9a-f]{64}$"
)
_FLAVORS = frozenset(("v2", "v3"))


@dataclass(frozen=True)
class ReleaseCacheIdentity:
    schema_version: int
    flavor: str
    source_version: str
    source_dsc_sha256: str
    dkc_revision: int
    build_policy_sha256: str
    validation_policy_sha256: str
    lto_mode: str

    def __post_init__(self) -> None:
        if self.schema_version != RELEASE_CACHE_REVISION:
            raise ValueError("release-cache schema revision is unsupported")
        if self.flavor not in _FLAVORS:
            raise ValueError("release cache is limited to release flavors")
        if not self.source_version or self.dkc_revision < 1:
            raise ValueError("release-cache source identity is incomplete")
        for value in (
            self.source_dsc_sha256,
            self.build_policy_sha256,
            self.validation_policy_sha256,
        ):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError("release-cache identity contains a malformed digest")
        if self.lto_mode not in ("none", "thin", "full"):
            raise ValueError("release-cache LTO mode is invalid")

    def digest(self) -> str:
        return sha256_of(asdict(self))

    def key(self) -> str:
        return f"dkc-release-v{self.schema_version}-{self.flavor}-{self.digest()}"


def release_cache_identity(
    decision: DiscoveryDecision,
    *,
    flavor: str,
    repository_root: pathlib.Path,
) -> ReleaseCacheIdentity:
    return ReleaseCacheIdentity(
        schema_version=RELEASE_CACHE_REVISION,
        flavor=flavor,
        source_version=decision.source_version,
        source_dsc_sha256=decision.source_dsc_sha256,
        dkc_revision=decision.dkc_revision,
        build_policy_sha256=decision.build_policy_sha256,
        validation_policy_sha256=validation_policy_digest(repository_root),
        lto_mode=decision.lto_mode,
    )


def _read_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} is not an object")
    return value


def _read_environment(path: pathlib.Path, label: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{label} is not readable text") from exc
    for line in lines:
        key, separator, value = line.partition("=")
        if not separator or not key or key in values or "\x00" in value:
            raise ValueError(f"{label} is malformed")
        values[key] = value
    return values


def _require_fields(value: dict[str, Any], expected: dict[str, Any], label: str) -> None:
    mismatched = sorted(key for key, field in expected.items() if value.get(key) != field)
    if mismatched:
        raise ValueError(f"{label} differs in: {', '.join(mismatched)}")


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_semantics(
    cache_root: pathlib.Path,
    identity: ReleaseCacheIdentity,
    build_image: str,
) -> None:
    flavor_root = cache_root / "flavor"
    selftest_root = cache_root / "kselftest"
    qemu_root = cache_root / "qemu"
    for path, label in (
        (flavor_root, "flavor"),
        (selftest_root, "selftest"),
        (qemu_root, "QEMU"),
    ):
        if not path.is_dir() or path.is_symlink():
            raise ValueError(f"release cache lacks its {label} result")

    flavor_status = _read_environment(
        flavor_root / "evidence/result.env", "flavor result"
    )
    _require_fields(
        flavor_status,
        {"status": "PASS", "flavor": identity.flavor, "lto_mode": identity.lto_mode},
        "flavor result",
    )
    publication = _read_json(
        flavor_root / "evidence/publication-identity.json", "publication identity"
    )
    build_inputs = publication.get("build_inputs")
    if not isinstance(build_inputs, dict):
        raise ValueError("publication identity lacks build inputs")
    source = build_inputs.get("debian_source")
    if not isinstance(source, dict):
        raise ValueError("publication identity lacks Debian source inputs")
    _require_fields(
        source,
        {"version": identity.source_version, "dsc_sha256": identity.source_dsc_sha256},
        "publication source identity",
    )
    _require_fields(
        build_inputs,
        {
            "dkc_revision": identity.dkc_revision,
            "overlay_sha256": identity.build_policy_sha256,
            "lto_mode": identity.lto_mode,
        },
        "publication build identity",
    )
    build_input_digest = publication.get("build_input_digest")
    if not isinstance(build_input_digest, str) or not _SHA256_RE.fullmatch(
        build_input_digest
    ):
        raise ValueError("publication build-input digest is malformed")
    releases = publication.get("kernel_releases")
    if not isinstance(releases, dict) or not isinstance(
        releases.get(identity.flavor), str
    ):
        raise ValueError("publication identity lacks the flavor kernel release")
    kernel_release = releases[identity.flavor]

    provenance = _read_environment(
        flavor_root / "evidence/build-image-provenance.env", "build image provenance"
    )
    match = _BUILD_IMAGE_RE.fullmatch(build_image)
    assert match is not None
    _require_fields(
        provenance,
        {"provider": "registry", "registry_manifest_digest": match.group(1)},
        "build image provenance",
    )
    gates = _read_environment(
        flavor_root / "evidence/post-build-gates.env", "post-build gates"
    )
    if gates != {
        "package_attestation_rc": "0",
        "kbuild_audit_rc": "0",
        "simd_audit_rc": "0",
        "lintian_rc": "0",
    }:
        raise ValueError("one or more cached post-build gates did not pass")
    for name, label in (
        ("kernel-simd-audit.json", "SIMD audit"),
        ("kbuild-command-audit.json", "Kbuild audit"),
    ):
        report = _read_json(flavor_root / "evidence" / name, label)
        _require_fields(report, {"status": "PASS", "lto_mode": identity.lto_mode}, label)

    attestation = _read_json(
        flavor_root / "evidence/attestation.json", "kernel attestation"
    )
    _require_fields(
        attestation,
        {
            "status": "PASS",
            "flavor": identity.flavor,
            "kernel_release": kernel_release,
            "lto_mode": identity.lto_mode,
        },
        "kernel attestation",
    )
    packages = attestation.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise ValueError("kernel attestation contains no package inventory")
    observed_debs = {path.name: path for path in (flavor_root / "artifacts").glob("*.deb")}
    if set(observed_debs) != set(packages):
        raise ValueError("cached binary package set differs from its attestation")
    for name, digest in packages.items():
        if not isinstance(digest, str) or _sha256(observed_debs[name]) != digest:
            raise ValueError("cached binary package digest differs from its attestation")

    selftest_status = _read_environment(
        selftest_root / "evidence/result.env", "selftest result"
    )
    _require_fields(
        selftest_status,
        {
            "status": "PASS",
            "flavor": identity.flavor,
            "profile_kind": "qualification",
            "kernel_release": kernel_release,
            "lto_mode": identity.lto_mode,
        },
        "selftest result",
    )
    selftest = _read_json(
        selftest_root / "evidence/kselftest-build.json", "selftest build"
    )
    _require_fields(
        selftest,
        {
            "status": "PASS",
            "flavor": identity.flavor,
            "profile_kind": "qualification",
            "kernel_release": kernel_release,
            "lto_mode": identity.lto_mode,
            "build_input_digest": build_input_digest,
        },
        "selftest build",
    )

    qemu_status = _read_environment(qemu_root / "evidence/result.env", "QEMU result")
    _require_fields(
        qemu_status,
        {
            "status": "PASS",
            "flavor": identity.flavor,
            "lto_mode": identity.lto_mode,
            "accelerator": "kvm",
        },
        "QEMU result",
    )
    scenario = _read_json(
        qemu_root / identity.flavor / "evidence/result.json", "QEMU scenario"
    )
    _require_fields(
        scenario,
        {
            "status": "PASS",
            "flavor": identity.flavor,
            "accelerator": "kvm",
            "qemu_exit": 0,
        },
        "QEMU scenario",
    )
    guest = _read_environment(
        qemu_root / identity.flavor / "guest/result.env", "guest qualification"
    )
    _require_fields(
        guest,
        {
            "status": "PASS",
            "flavor": identity.flavor,
            "target_kernel": kernel_release,
            "qualification": "PASS",
        },
        "guest qualification",
    )


_CACHE_PAYLOAD = {
    "flavor": "flavor",
    "kselftest": "kselftest",
    "qemu": "qemu",
}


def _current_manifest(
    identity: ReleaseCacheIdentity,
    expected_key: str,
    *,
    build_image: str,
    toolbox_image: str,
) -> dict[str, object]:
    if not _BUILD_IMAGE_RE.fullmatch(build_image):
        raise ValueError("release cache requires an immutable build-image provenance")
    if not _TOOLBOX_IMAGE_RE.fullmatch(toolbox_image):
        raise ValueError("release cache requires an immutable toolbox provenance")
    return {
        "schema": "dkc.release-cache.v2",
        "cache_key": expected_key,
        "identity": asdict(identity),
        "provenance": {
            "build_image": build_image,
            "toolbox_image": toolbox_image,
        },
        "payload": _CACHE_PAYLOAD,
    }


def _expected_identity(
    decision_root: pathlib.Path,
    flavor: str,
    expected_key: str,
    repository_root: pathlib.Path,
) -> ReleaseCacheIdentity:
    if not expected_key or len(expected_key) > 512:
        raise ValueError("expected release-cache key is invalid")
    identity = release_cache_identity(
        load_discovery_decision(decision_root),
        flavor=flavor,
        repository_root=repository_root,
    )
    if identity.key() != expected_key:
        raise ValueError("release-cache key differs from the lifecycle decision")
    return identity


def _verify_candidate(
    cache_root: pathlib.Path,
    identity: ReleaseCacheIdentity,
    expected_key: str,
) -> None:
    verify_evidence_directory(cache_root)
    manifest = _read_json(cache_root / "cache.json", "release-cache manifest")
    if set(manifest) != {"schema", "cache_key", "identity", "provenance", "payload"}:
        raise ValueError("release-cache manifest has unexpected fields")
    if (
        manifest.get("schema") != "dkc.release-cache.v2"
        or manifest.get("cache_key") != expected_key
        or manifest.get("identity") != asdict(identity)
        or manifest.get("payload") != _CACHE_PAYLOAD
    ):
        raise ValueError("release-cache manifest differs from its expected identity")
    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "build_image",
        "toolbox_image",
    }:
        raise ValueError("release-cache provenance is malformed")
    build_image = provenance.get("build_image")
    toolbox_image = provenance.get("toolbox_image")

    if not isinstance(build_image, str) or not _BUILD_IMAGE_RE.fullmatch(build_image):
        raise ValueError("release-cache build-image provenance is malformed")
    if not isinstance(toolbox_image, str) or not _TOOLBOX_IMAGE_RE.fullmatch(toolbox_image):
        raise ValueError("release-cache toolbox provenance is malformed")
    _verify_semantics(cache_root, identity, build_image)


def verify_release_cache(
    cache_root: pathlib.Path,
    *,
    decision_root: pathlib.Path,
    flavor: str,
    expected_key: str,
    repository_root: pathlib.Path,
    cache_workspace: pathlib.Path | None = None,
) -> ReleaseCacheIdentity:
    _require_cache_location(cache_root, cache_workspace or repository_root, flavor)
    if not cache_root.is_dir() or cache_root.is_symlink():
        raise ValueError("release-cache root is not a plain directory")
    identity = _expected_identity(
        decision_root, flavor, expected_key, repository_root
    )
    _verify_candidate(cache_root, identity, expected_key)
    return identity


def _require_plain_tree(root: pathlib.Path, label: str) -> None:
    if not root.is_dir() or root.is_symlink():
        raise ValueError(f"{label} is not a plain directory")
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise ValueError(f"{label} contains a link or special file")


def _require_cache_location(
    cache_root: pathlib.Path, repository_root: pathlib.Path, flavor: str
) -> None:
    if flavor not in _FLAVORS:
        raise ValueError("release cache is limited to release flavors")
    lexical_root = pathlib.Path(os.path.abspath(repository_root))
    if not lexical_root.is_dir() or lexical_root.is_symlink():
        raise ValueError("release-cache repository root is not a plain directory")
    expected = lexical_root / "out/release-cache" / flavor
    actual = pathlib.Path(os.path.abspath(cache_root))
    if actual != expected:
        raise ValueError("release-cache path is outside its confined root")
    current = lexical_root
    for component in ("out", "release-cache", flavor):
        current /= component
        if current.is_symlink():
            raise ValueError("release-cache path traverses a symbolic link")


def _write_inventory(root: pathlib.Path) -> None:
    records: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("release-cache staging contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("release-cache staging contains a special file")
        relative = path.relative_to(root).as_posix()
        if relative != "evidence.sha256":
            records.append(f"{_sha256(path)}  {relative}")
    if not records:
        raise ValueError("release-cache staging is empty")
    (root / "evidence.sha256").write_text("\n".join(records) + "\n", encoding="utf-8")


def prepare_release_cache(
    cache_root: pathlib.Path,
    *,
    flavor_result: pathlib.Path,
    selftest_result: pathlib.Path,
    qemu_result: pathlib.Path,
    decision_root: pathlib.Path,
    flavor: str,
    build_image: str,
    toolbox_image: str,
    expected_key: str,
    repository_root: pathlib.Path,
    cache_workspace: pathlib.Path | None = None,
) -> ReleaseCacheIdentity:
    workspace = cache_workspace or repository_root
    _require_cache_location(cache_root, workspace, flavor)
    decision = load_discovery_decision(decision_root)
    if decision.decision != "build" or not decision.build_required:
        raise ValueError("release cache can only be prepared for a build decision")
    identity = release_cache_identity(
        decision, flavor=flavor, repository_root=repository_root
    )
    if identity.key() != expected_key:
        raise ValueError("release-cache key differs from the lifecycle decision")
    if cache_root.exists() or cache_root.is_symlink():
        return verify_release_cache(
            cache_root,
            decision_root=decision_root,
            flavor=flavor,
            expected_key=expected_key,
            repository_root=repository_root,
            cache_workspace=workspace,
        )
    sources = {
        "flavor": flavor_result,
        "kselftest": selftest_result,
        "qemu": qemu_result,
    }
    for label, source in sources.items():
        _require_plain_tree(source, label)
    cache_root.parent.mkdir(parents=True, exist_ok=True)
    temporary = pathlib.Path(
        tempfile.mkdtemp(prefix=f".{flavor}-", dir=cache_root.parent)
    )
    try:
        for label, source in sources.items():
            shutil.copytree(source, temporary / label, copy_function=shutil.copy2)
        manifest = _current_manifest(
            identity,
            expected_key,
            build_image=build_image,
            toolbox_image=toolbox_image,
        )
        (temporary / "cache.json").write_text(dumps(manifest), encoding="utf-8")
        _write_inventory(temporary)
        os.replace(temporary, cache_root)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return verify_release_cache(
        cache_root,
        decision_root=decision_root,
        flavor=flavor,
        expected_key=expected_key,
        repository_root=repository_root,
        cache_workspace=workspace,
    )
