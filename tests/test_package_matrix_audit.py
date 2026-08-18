"""Pure policy checks for supported builds and the release package matrix."""

from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tarfile

import pytest


ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "in-container" / "audit-package-matrix.py"
spec = importlib.util.spec_from_file_location("audit_package_matrix", SCRIPT)
assert spec and spec.loader
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def _identity() -> dict[str, object]:
    abi = "7.1.7+dkc13.r1.g0123456789ab"
    versioned = []
    meta = []
    for flavor in audit.FLAVORS:
        suffix = f"{flavor}-amd64"
        versioned.extend(
            f"dkc-linux-{role}-{abi}-{suffix}"
            for role in ("base", "binary", "modules", "image", "headers")
        )
        meta.extend(
            f"dkc-linux-{role}-{suffix}" for role in ("base", "image", "headers")
        )
    versioned.extend(
        (f"dkc-linux-headers-{abi}-common", f"dkc-linux-kbuild-{abi}")
    )
    return {
        "schema_version": 1,
        "source_package": "dkc-linux",
        "debian_source_version": "7.1.7-1",
        "build_input_digest": "f" * 64,
        "abi": abi,
        "package_version": "7.1.7-1+dkc13.1",
        "lto_mode": "thin",
        "kernel_releases": {
            flavor: f"{abi}-{flavor}-amd64" for flavor in audit.FLAVORS
        },
        "package_names": {
            "versioned": versioned,
            "meta": meta,
            "keyring": ["dkc-archive-keyring"],
        },
    }


def _write(package_root: pathlib.Path, relative: str, text: str = "fixture\n") -> None:
    path = package_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _symlink(package_root: pathlib.Path, relative: str, target: str) -> None:
    path = package_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)


def _add_kernel_payload(
    package_root: pathlib.Path,
    package: str,
    identity: dict[str, object],
    *,
    header_vmlinux: bool | None = None,
) -> None:
    abi = str(identity["abi"])
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    common = f"dkc-linux-headers-{abi}-common"
    kbuild = f"dkc-linux-kbuild-{abi}"
    if package == common:
        root = f"usr/src/linux-headers-{abi}-common"
        _write(package_root, f"{root}/Makefile")
        for leaf in ("scripts", "tools"):
            _symlink(package_root, f"{root}/{leaf}", f"../../lib/linux-kbuild-{abi}/{leaf}")
    elif package == kbuild:
        _write(package_root, f"usr/lib/linux-kbuild-{abi}/scripts/basic/fixdep")
        _write(package_root, f"usr/lib/linux-kbuild-{abi}/tools/objtool/objtool")
        _symlink(
            package_root,
            f"usr/src/linux-kbuild-{abi}",
            f"../lib/linux-kbuild-{abi}",
        )

    for flavor in audit.FLAVORS:
        krel = str(releases[flavor])
        if package == f"dkc-linux-base-{krel}":
            _write(package_root, f"boot/config-{krel}")
            _write(package_root, f"boot/System.map-{krel}")
        elif package == f"dkc-linux-binary-{krel}":
            _write(package_root, f"boot/vmlinuz-{krel}")
        elif package == f"dkc-linux-modules-{krel}":
            _write(package_root, f"usr/lib/modules/{krel}/kernel/drivers/dkc_fixture.ko")
        elif package == f"dkc-linux-headers-{krel}":
            root = f"usr/src/linux-headers-{krel}"
            for leaf in (".kernelvariables", "Makefile", "Module.symvers"):
                _write(package_root, f"{root}/{leaf}")
            include_header_vmlinux = (
                identity["lto_mode"] == "none"
                if header_vmlinux is None
                else header_vmlinux
            )
            if include_header_vmlinux:
                _write(package_root, f"{root}/vmlinux")
            _symlink(
                package_root,
                f"usr/lib/modules/{krel}/build",
                f"../../../src/linux-headers-{krel}",
            )
            _symlink(
                package_root,
                f"usr/lib/modules/{krel}/source",
                f"../../../src/linux-headers-{abi}-common",
            )


def _add_copyright_documentation(
    package_root: pathlib.Path, package: str, identity: dict[str, object]
) -> None:
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    target = None
    for flavor, krel_value in releases.items():
        krel = str(krel_value)
        base = f"dkc-linux-base-{krel}"
        if package in {
            f"dkc-linux-binary-{krel}",
            f"dkc-linux-modules-{krel}",
            f"dkc-linux-image-{krel}",
            f"dkc-linux-headers-{krel}",
        }:
            target = base
        elif package in {
            f"dkc-linux-base-{flavor}-amd64",
            f"dkc-linux-image-{flavor}-amd64",
            f"dkc-linux-headers-{flavor}-amd64",
        }:
            target = f"dkc-linux-base-{flavor}-amd64"
            if package == target:
                target = base
    root = f"usr/share/doc/{package}"
    if target is None:
        _write(package_root, f"{root}/copyright", "synthetic license fixture\n")
    else:
        _symlink(package_root, root, target)


def _add_control_contract(
    package_root: pathlib.Path,
    package: str,
    identity: dict[str, object],
) -> None:
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    for krel_value in releases.values():
        krel = str(krel_value)
        scripts: dict[str, str] = {}
        if package == f"dkc-linux-binary-{krel}":
            scripts = {
                "postrm": (
                    f'version={krel}\nif [ "$1" = remove ]; then\n'
                    "  linux-run-hooks image postrm\nfi\n"
                ),
            }
        elif package == f"dkc-linux-image-{krel}":
            scripts = {
                "preinst": f"version={krel}\nlinux-run-hooks image preinst\n",
                "postinst": f"version={krel}\nlinux-update-symlinks\nlinux-run-hooks image postinst\n",
                "prerm": f"version={krel}\nlinux-check-removal\nlinux-run-hooks image prerm\n",
                "postrm": (
                    f'version={krel}\nlinux-update-symlinks remove\ncase "$1" in\n'
                    "remove|purge)\n  ;;\n*)\n"
                    "  linux-run-hooks image postrm\n  ;;\nesac\n"
                ),
            }
        elif package == f"dkc-linux-modules-{krel}":
            scripts = {
                "postinst": f'version={krel}\ndepmod "$version"\n',
                "prerm": f'version={krel}\n# preserve modules.builtin\n',
            }
        elif package == f"dkc-linux-headers-{krel}":
            scripts = {
                "postinst": f"version={krel}\nlinux-run-hooks headers postinst\n",
            }
        for name, text in scripts.items():
            path = package_root / "DEBIAN" / name
            path.write_text("#!/bin/sh -e\n" + text + "exit 0\n", encoding="utf-8")
            path.chmod(0o755)
    for flavor, krel_value in releases.items():
        krel = str(krel_value)
        target = None
        if package == f"dkc-linux-image-{flavor}-amd64":
            target = f"dkc-linux-image-{krel}"
        elif package == f"dkc-linux-headers-{flavor}-amd64":
            target = f"dkc-linux-headers-{krel}"
        if target is None:
            continue
        for name in ("preinst", "postinst", "prerm", "postrm"):
            path = package_root / "DEBIAN" / name
            path.write_text(
                "#!/bin/sh\nset -e\n"
                "dpkg-maintscript-helper dir_to_symlink "
                f"/usr/share/doc/{package} {target} "
                f"5.7~rc5-1~exp1 {package} -- \"$@\"\n",
                encoding="utf-8",
            )
            path.chmod(0o755)


def _source_checksum(path: pathlib.Path) -> str:
    return f" {hashlib.sha256(path.read_bytes()).hexdigest()} {path.stat().st_size} {path.name}"


def _source_md5(path: pathlib.Path) -> str:
    digest = hashlib.md5(path.read_bytes(), usedforsecurity=False).hexdigest()
    return f" {digest} {path.stat().st_size} {path.name}"


def _build_synthetic_source_bundle(
    root: pathlib.Path, identity: dict[str, object]
) -> dict[str, object]:
    root.mkdir()
    version = str(identity["package_version"])
    prefix = f"dkc-linux_{version}"
    orig = root / "dkc-linux_7.1.7.orig.tar.xz"
    debian = root / f"{prefix}.debian.tar.xz"
    orig.write_bytes(b"synthetic orig\n")
    debian.write_bytes(b"synthetic debian\n")
    names = identity["package_names"]
    assert isinstance(names, dict)
    binaries = [*names["versioned"], *names["meta"]]
    dsc = root / f"{prefix}.dsc"
    dsc.write_text(
        "\n".join(
            (
                "Format: 3.0 (quilt)",
                "Source: dkc-linux",
                f"Binary: {', '.join(str(item) for item in binaries)}",
                "Architecture: any all",
                f"Version: {version}",
                "Maintainer: DKC Kernel Maintainers <build@dkc.invalid>",
                "Files:",
                _source_md5(orig),
                _source_md5(debian),
                "Checksums-Sha256:",
                _source_checksum(orig),
                _source_checksum(debian),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    buildinfo = root / f"{prefix}_source.buildinfo"
    buildinfo.write_text(
        "\n".join(
            (
                "Format: 1.0",
                "Source: dkc-linux",
                f"Version: {version}",
                "Architecture: source",
                "Build-Date: 2026-08-13 00:00:00+00:00",
                "Checksums-Sha256:",
                _source_checksum(dsc),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    changes = root / f"{prefix}_source.changes"
    changes.write_text(
        "\n".join(
            (
                "Format: 1.8",
                "Source: dkc-linux",
                f"Version: {version}",
                "Architecture: source",
                "Distribution: trixie",
                "Checksums-Sha256:",
                _source_checksum(dsc),
                _source_checksum(orig),
                _source_checksum(debian),
                _source_checksum(buildinfo),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    bundle = audit.validate_source_bundle(
        root,
        package="dkc-linux",
        version=version,
        upstream_version="7.1.7",
        expected_binary_packages=[str(item) for item in binaries],
    )
    report = bundle.to_dict()
    report.update(
        {
            "build_input_digest": identity["build_input_digest"],
            "reconstruction": "PASS",
            "source_tree_entries": 1,
            "source_tree_manifest_sha256": hashlib.sha256(b"fixture\n").hexdigest(),
            "status": "PASS",
        }
    )
    return report


def _build_synthetic_matrix(
    tmp_path: pathlib.Path,
    *,
    identity: dict[str, object] | None = None,
    conflicts: bool = False,
    duplicate_payload: bool = False,
    loose_dependency: bool = False,
    renamed_kbuild_payload: bool = False,
    unreviewed_payload: bool = False,
    divergent_common: bool = False,
    header_vmlinux: bool | None = None,
) -> list[pathlib.Path]:
    identity = identity or _identity()
    version = str(identity["package_version"])
    roots: list[pathlib.Path] = []
    package_number = 0
    abi = str(identity["abi"])
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    dependency_graph = audit.expected_internal_dependencies(
        abi, {str(key): str(value) for key, value in releases.items()}
    )
    internal_provides = audit.expected_internal_provides(
        {str(key): str(value) for key, value in releases.items()}
    )
    common_packages = {
        f"dkc-linux-headers-{abi}-common",
        f"dkc-linux-kbuild-{abi}",
    }
    loosened = False
    for flavor in audit.FLAVORS:
        root = tmp_path / flavor
        artifacts = root / "artifacts"
        evidence = root / "evidence"
        artifacts.mkdir(parents=True)
        evidence.mkdir()
        source_report = _build_synthetic_source_bundle(root / "source", identity)
        digests: dict[str, str] = {}
        for index, package in enumerate(sorted(audit.expected_for_flavor(identity, flavor))):
            package_root = tmp_path / f"package-{flavor}-{index}"
            control = package_root / "DEBIAN" / "control"
            payload = package_root / "usr" / "share" / "dkc-matrix-test" / package
            control.parent.mkdir(parents=True)
            control.parent.chmod(0o755)
            payload.parent.mkdir(parents=True)
            conflict_field = "Conflicts: linux-image-amd64\n" if conflicts and package_number == 0 else ""
            dependencies = []
            for related, exact in sorted(dependency_graph[package].items()):
                relation = f"{related} (= {version})" if exact else related
                if loose_dependency and exact and not loosened:
                    relation = related
                    loosened = True
                dependencies.append(relation)
            depends_field = f"Depends: {', '.join(dependencies)}\n" if dependencies else ""
            provides = sorted(internal_provides.get(package, set()))
            provides_field = f"Provides: {', '.join(provides)}\n" if provides else ""
            control.write_text(
                f"Package: {package}\n"
                f"Version: {version}\n"
                "Source: dkc-linux\n"
                f"Architecture: {'all' if package.endswith('-common') else 'amd64'}\n"
                "Maintainer: DKC Build Service <build@dkc.invalid>\n"
                f"{conflict_field}"
                f"{depends_field}"
                f"{provides_field}"
                "Description: synthetic matrix audit fixture\n",
                encoding="utf-8",
            )
            payload_flavor = "common" if package in common_packages else flavor
            if (
                divergent_common
                and package in common_packages
                and flavor == audit.RELEASE_FLAVORS[-1]
            ):
                payload_flavor = "divergent"
            payload.write_text(f"{payload_flavor} {package}\n", encoding="utf-8")
            if duplicate_payload and package_number < 2:
                _write(package_root, "usr/share/dkc-matrix-test/shared-collision")
            if unreviewed_payload and package_number == 0:
                _write(package_root, "boot/unreviewed-kernel-payload")
            _add_kernel_payload(
                package_root,
                package,
                identity,
                header_vmlinux=header_vmlinux,
            )
            _add_copyright_documentation(package_root, package, identity)
            if renamed_kbuild_payload and package == f"dkc-linux-kbuild-{abi}":
                conventional_lib = package_root / f"usr/lib/linux-kbuild-{abi}"
                renamed_lib = package_root / f"usr/lib/dkc-linux-kbuild-{abi}"
                conventional_lib.rename(renamed_lib)
                conventional_source = package_root / f"usr/src/linux-kbuild-{abi}"
                conventional_source.unlink()
                _symlink(
                    package_root,
                    f"usr/src/dkc-linux-kbuild-{abi}",
                    f"../lib/dkc-linux-kbuild-{abi}",
                )
            _add_control_contract(package_root, package, identity)
            deb = artifacts / f"{package}_{version}_amd64.deb"
            subprocess.run(
                ["dpkg-deb", "--build", "--root-owner-group", package_root, deb],
                check=True,
                capture_output=True,
                env={**os.environ, "SOURCE_DATE_EPOCH": "1760000000"},
            )
            digest = hashlib.sha256(deb.read_bytes()).hexdigest()
            digests[deb.name] = digest
            package_number += 1

        (evidence / "result.env").write_text(
            f"status=PASS\nflavor={flavor}\n", encoding="utf-8"
        )
        (evidence / "publication-identity.json").write_text(
            json.dumps(identity, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        (evidence / "attestation.json").write_text(
            json.dumps(
                {
                    "status": "PASS",
                    "flavor": flavor,
                    "kernel_release": identity["kernel_releases"][flavor],
                    "lto_mode": identity["lto_mode"],
                    "btf_policy": (
                        "required" if identity["lto_mode"] == "none" else "forbidden"
                    ),
                    "packages": digests,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        source_evidence = evidence / "source-package"
        source_evidence.mkdir()
        (source_evidence / "source-package.json").write_text(
            json.dumps(source_report, separators=(",", ":"), sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _write_evidence_manifest(evidence)
        roots.append(root)
    return roots


def _write_evidence_manifest(evidence: pathlib.Path) -> None:
    records = []
    for path in sorted(item for item in evidence.rglob("*") if item.is_file()):
        if path.name == "evidence.sha256":
            continue
        relative = path.relative_to(evidence).as_posix()
        records.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  ./{relative}\n")
    (evidence / "evidence.sha256").write_text("".join(records), encoding="utf-8")


def _run_audit(
    tmp_path: pathlib.Path, roots: list[pathlib.Path]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            SCRIPT,
            *roots[: len(audit.RELEASE_FLAVORS)],
            tmp_path / "repository",
            tmp_path / "evidence" / "matrix.json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def _run_flavor_audit(
    tmp_path: pathlib.Path, flavor: str, root: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            SCRIPT,
            "--flavor",
            flavor,
            root,
            tmp_path / "flavor-repository",
            tmp_path / "flavor-evidence" / "audit.json",
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_every_flavor_export_is_self_contained() -> None:
    identity = _identity()
    selected = {
        flavor: audit.expected_for_flavor(identity, flavor) for flavor in audit.FLAVORS
    }
    assert all(len(selected[flavor]) == 10 for flavor in audit.FLAVORS)
    assert len(set().union(*selected.values())) == 26
    common = set.intersection(*selected.values())
    assert common == {
        f"dkc-linux-headers-{identity['abi']}-common",
        f"dkc-linux-kbuild-{identity['abi']}",
    }


def test_release_synthetic_matrix_builds_one_flat_repository(
    tmp_path: pathlib.Path,
) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    result = _run_audit(tmp_path, roots)
    assert result.returncode == 0, result.stderr
    repository = tmp_path / "repository"
    report = tmp_path / "evidence" / "matrix.json"
    parsed = json.loads(report.read_text(encoding="utf-8"))
    assert parsed["status"] == "PASS"
    assert parsed["release_flavors"] == list(audit.RELEASE_FLAVORS)
    assert parsed["input_package_count"] == 20
    assert parsed["package_count"] == 18
    assert parsed["common_package_canonical_flavor"] == "v2"
    assert all(
        set(copies) == set(audit.RELEASE_FLAVORS)
        for copies in parsed["common_package_copies"].values()
    )
    assert parsed["payload_collisions"] == 0
    assert len(list(repository.glob("*.deb"))) == 18
    assert not any("-v4-amd64" in name for name in parsed["repository_packages"])
    assert (repository / "Packages").is_file()
    assert (repository / "Sources").is_file()
    assert len(list(repository.glob("*.dsc"))) == 1
    assert len(parsed["source_repository_files"]) == 5
    assert set(parsed["source_report_sha256"]) == set(audit.RELEASE_FLAVORS)
    assert set(parsed["source_upload_metadata"]) == set(audit.RELEASE_FLAVORS)
    assert parsed["payload_layout"] == "PASS"
    assert parsed["copyright_documentation"] == "PASS"
    assert (report.parent / parsed["payload_inventory"]).is_file()


def test_package_matrix_matches_header_vmlinux_to_btf_policy(
    tmp_path: pathlib.Path,
) -> None:
    non_lto = _identity()
    non_lto["lto_mode"] = "none"
    roots = _build_synthetic_matrix(tmp_path / "non-lto", identity=non_lto)
    result = _run_audit(tmp_path / "non-lto", roots)
    assert result.returncode == 0, result.stderr

    roots = _build_synthetic_matrix(tmp_path / "thin-extra", header_vmlinux=True)
    result = _run_audit(tmp_path / "thin-extra", roots)
    assert result.returncode != 0
    assert "contains a vmlinux payload while BTF is disabled" in result.stderr

    roots = _build_synthetic_matrix(
        tmp_path / "non-lto-missing",
        identity=non_lto,
        header_vmlinux=False,
    )
    result = _run_audit(tmp_path / "non-lto-missing", roots)
    assert result.returncode != 0
    assert "does not own required file payload" in result.stderr


def test_source_upload_metadata_may_record_distinct_build_time(
    tmp_path: pathlib.Path,
) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    source_report_path = (
        roots[1] / "evidence" / "source-package" / "source-package.json"
    )
    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    source_root = roots[1] / "source"
    buildinfo = source_root / str(source_report["buildinfo"])
    buildinfo.write_text(
        buildinfo.read_text(encoding="utf-8").replace(
            "Build-Date: 2026-08-13", "Build-Date: 2026-08-14"
        ),
        encoding="utf-8",
    )
    changes = source_root / str(source_report["changes"])
    changes.write_text(
        "\n".join(
            _source_checksum(buildinfo)
            if line.endswith(f" {buildinfo.name}")
            else line
            for line in changes.read_text(encoding="utf-8").splitlines()
        )
        + "\n",
        encoding="utf-8",
    )
    identity = _identity()
    names = identity["package_names"]
    assert isinstance(names, dict)
    updated = audit.validate_source_bundle(
        source_root,
        package="dkc-linux",
        version=str(identity["package_version"]),
        upstream_version="7.1.7",
        expected_binary_packages=[*names["versioned"], *names["meta"]],
    ).to_dict()
    for key in (
        "build_input_digest",
        "reconstruction",
        "source_tree_entries",
        "source_tree_manifest_sha256",
        "status",
    ):
        updated[key] = source_report[key]
    source_report_path.write_text(
        json.dumps(updated, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_evidence_manifest(roots[1] / "evidence")

    result = _run_audit(tmp_path, roots)
    assert result.returncode == 0, result.stderr
    matrix = json.loads(
        (tmp_path / "evidence" / "matrix.json").read_text(encoding="utf-8")
    )
    metadata_names = {str(updated["buildinfo"]), str(updated["changes"])}
    expected_hashes = {
        hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        for name in metadata_names
    }
    assert {
        details["sha256"]
        for details in matrix["source_upload_metadata"]["v3"].values()
    } == expected_hashes


def test_every_flavor_must_carry_its_physical_source_bundle(
    tmp_path: pathlib.Path,
) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    source = roots[1] / "source"
    for path in source.iterdir():
        path.unlink()
    source.rmdir()

    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "v3 export lacks a plain source bundle directory" in result.stderr


def test_reproducible_source_member_difference_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    source_report_path = (
        roots[1] / "evidence" / "source-package" / "source-package.json"
    )
    source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
    for record in source_report["files"]:
        if record["name"] == source_report["dsc"]:
            record["sha256"] = "0" * 64
    source_report_path.write_text(
        json.dumps(source_report, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_evidence_manifest(roots[1] / "evidence")

    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "reproducible source member differs from v2" in result.stderr


@pytest.mark.parametrize(("flavor", "root_index"), (("v3", 1), ("v4", 2)))
def test_supported_flavor_export_builds_a_ten_package_vm_input_directory(
    tmp_path: pathlib.Path, flavor: str, root_index: int
) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    result = _run_flavor_audit(tmp_path, flavor, roots[root_index])
    assert result.returncode == 0, result.stderr
    repository = tmp_path / "flavor-repository"
    report = json.loads(
        (tmp_path / "flavor-evidence" / "audit.json").read_text(encoding="utf-8")
    )
    assert report["status"] == "PASS"
    assert report["flavor"] == flavor
    assert report["package_count"] == 10
    assert report["internal_dependency_graph"] == "PASS"
    assert len(list(repository.glob("*.deb"))) == 10
    assert report["install_method"] == "direct-dpkg"
    assert not (repository / "Packages").exists()


def test_nonidentical_common_package_copy_is_rejected(tmp_path: pathlib.Path) -> None:
    roots = _build_synthetic_matrix(tmp_path, divergent_common=True)
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "common package copy differs from canonical v2 bytes" in result.stderr


def test_malformed_kernel_release_identity_fails_cleanly(tmp_path: pathlib.Path) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    identity = _identity()
    identity["kernel_releases"] = ["not", "an", "object"]
    for root in roots:
        (root / "evidence" / "publication-identity.json").write_text(
            json.dumps(identity, sort_keys=True) + "\n", encoding="utf-8"
        )
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "kernel_releases must contain exactly" in result.stderr
    assert "Traceback" not in result.stderr


def test_evidence_readers_reject_symlinked_records(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    link = tmp_path / "record.json"
    link.symlink_to(target)
    with pytest.raises(SystemExit, match="not a plain file"):
        audit.load_json(link)


def test_unmanifested_evidence_is_rejected(tmp_path: pathlib.Path) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    (roots[0] / "evidence" / "unexpected.txt").write_text("extra\n", encoding="utf-8")
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "evidence manifest file set differs" in result.stderr


def test_package_identity_requires_exact_kernel_graph_and_separate_keyring() -> None:
    identity = _identity()
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    validated = audit.validate_publication_identity(identity)
    assert len(validated[3]) == 26

    package_names = identity["package_names"]
    assert isinstance(package_names, dict)
    package_names["keyring"] = []
    with pytest.raises(SystemExit, match="inventory is malformed"):
        audit.validate_publication_identity(identity)

    package_names["keyring"] = ["dkc-archive-keyring"]
    versioned = package_names["versioned"]
    assert isinstance(versioned, list)
    versioned[0] = f"dkc-linux-invented-{releases['v2']}"
    with pytest.raises(SystemExit, match="exact DKC package graph"):
        audit.validate_publication_identity(identity)


def test_attestation_krel_mismatch_is_rejected(tmp_path: pathlib.Path) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    path = roots[1] / "evidence" / "attestation.json"
    attestation = json.loads(path.read_text(encoding="utf-8"))
    attestation["kernel_release"] = "wrong-release"
    path.write_text(json.dumps(attestation, sort_keys=True) + "\n", encoding="utf-8")
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "attestation identity does not match" in result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    (("lto_mode", "none"), ("btf_policy", "required")),
)
def test_attestation_build_policy_mismatch_is_rejected(
    tmp_path: pathlib.Path, field: str, value: str
) -> None:
    roots = _build_synthetic_matrix(tmp_path)
    path = roots[1] / "evidence" / "attestation.json"
    attestation = json.loads(path.read_text(encoding="utf-8"))
    attestation[field] = value
    path.write_text(json.dumps(attestation, sort_keys=True) + "\n", encoding="utf-8")
    _write_evidence_manifest(roots[1] / "evidence")

    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "attestation identity does not match" in result.stderr


def test_conflicts_relation_is_rejected(tmp_path: pathlib.Path) -> None:
    roots = _build_synthetic_matrix(tmp_path, conflicts=True)
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "Conflicts/Replaces" in result.stderr


def test_loose_internal_dependency_is_rejected(tmp_path: pathlib.Path) -> None:
    roots = _build_synthetic_matrix(tmp_path, loose_dependency=True)
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "is not exactly" in result.stderr


def test_internal_dependency_alternative_is_rejected() -> None:
    identity = _identity()
    abi = str(identity["abi"])
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    release_map = {str(key): str(value) for key, value in releases.items()}
    graph = audit.expected_internal_dependencies(abi, release_map)
    version = str(identity["package_version"])
    rows = {
        package: {field: "" for field in audit.RELATION_FIELDS}
        for package in graph
    }
    for package, dependencies in graph.items():
        rows[package]["depends"] = ", ".join(
            f"{related} (= {version})" if exact else related
            for related, exact in dependencies.items()
        )
    for package, provides in audit.expected_internal_provides(release_map).items():
        rows[package]["provides"] = ", ".join(sorted(provides))
    package = next(name for name, dependencies in graph.items() if dependencies)
    rows[package]["depends"] += " | linux-image-amd64"
    with pytest.raises(SystemExit, match="alternative internal relation"):
        audit.validate_internal_dependency_graph(abi, version, release_map, rows)


def test_unrelated_dependency_alternative_is_accepted() -> None:
    identity = _identity()
    abi = str(identity["abi"])
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    release_map = {str(key): str(value) for key, value in releases.items()}
    graph = audit.expected_internal_dependencies(abi, release_map)
    version = str(identity["package_version"])
    rows = {
        package: {field: "" for field in audit.RELATION_FIELDS}
        for package in graph
    }
    for package, dependencies in graph.items():
        internal = [
            f"{related} (= {version})" if exact else related
            for related, exact in dependencies.items()
        ]
        rows[package]["depends"] = ", ".join(
            [*internal, "initramfs-tools | linux-initramfs-tool"]
        )
    for package, provides in audit.expected_internal_provides(release_map).items():
        rows[package]["provides"] = ", ".join(sorted(provides))
    audit.validate_internal_dependency_graph(abi, version, release_map, rows)


def test_missing_lifecycle_command_is_rejected() -> None:
    identity = _identity()
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    # Rebuilding a full .deb just to mutate its control archive would obscure
    # the policy under test, so exercise the fail-closed validator directly.
    package = f"dkc-linux-image-{releases['v2']}"
    with pytest.raises(SystemExit, match="lifecycle command"):
        audit.validate_maintainer_scripts(
            package,
            {str(key): str(value) for key, value in releases.items()},
            {
                "preinst": (0o755, f"version={releases['v2']}\nlinux-run-hooks image preinst"),
                "postinst": (0o755, f"version={releases['v2']}\n"),
                "prerm": (0o755, f"version={releases['v2']}\nlinux-check-removal\nlinux-run-hooks image prerm"),
                "postrm": (
                    0o755,
                    f'version={releases["v2"]}\nlinux-update-symlinks remove\n'
                    'case "$1" in\nremove|purge)\n  ;;\n*)\n'
                    "  linux-run-hooks image postrm\n  ;;\nesac",
                ),
            },
        )


def test_removal_hook_handoff_is_enforced() -> None:
    identity = _identity()
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    krel = str(releases["v2"])
    release_map = {str(key): str(value) for key, value in releases.items()}
    with pytest.raises(SystemExit, match="defer removal hooks"):
        audit.validate_maintainer_scripts(
            f"dkc-linux-image-{krel}",
            release_map,
            {
                "preinst": (0o755, f"version={krel}\nlinux-run-hooks image preinst"),
                "postinst": (
                    0o755,
                    f"version={krel}\nlinux-update-symlinks\nlinux-run-hooks image postinst",
                ),
                "prerm": (
                    0o755,
                    f"version={krel}\nlinux-check-removal\nlinux-run-hooks image prerm",
                ),
                "postrm": (
                    0o755,
                    f'version={krel}\nlinux-update-symlinks remove\ncase "$1" in\n'
                    "remove|purge)\n  linux-run-hooks image postrm\n  ;;\n*)\n"
                    "  linux-run-hooks image postrm\n  ;;\nesac",
                ),
            },
        )

    with pytest.raises(SystemExit, match="removal hook exactly once"):
        audit.validate_maintainer_scripts(
            f"dkc-linux-binary-{krel}",
            release_map,
            {
                "postrm": (
                    0o755,
                    f'version={krel}\nif [ "$1" = remove ]; then\n'
                    "  linux-run-hooks image postrm\n"
                    "  linux-run-hooks image postrm\nfi",
                ),
            },
        )


def test_unexpected_lifecycle_script_is_rejected() -> None:
    identity = _identity()
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    package = f"dkc-linux-base-{releases['v2']}"
    with pytest.raises(SystemExit, match="maintainer script set differs"):
        audit.validate_maintainer_scripts(
            package,
            {str(key): str(value) for key, value in releases.items()},
            {"postinst": (0o755, "#!/bin/sh\nexit 0\n")},
        )


def test_metapackage_documentation_transition_is_exact() -> None:
    identity = _identity()
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    release_map = {str(key): str(value) for key, value in releases.items()}
    package = "dkc-linux-headers-v2-amd64"
    correct = (
        "#!/bin/sh\nset -e\n"
        "dpkg-maintscript-helper dir_to_symlink "
        f"/usr/share/doc/{package} dkc-linux-headers-{releases['v2']} "
        f"5.7~rc5-1~exp1 {package} -- \"$@\"\n"
    )
    scripts = {name: (0o755, correct) for name in ("preinst", "postinst", "prerm", "postrm")}
    audit.validate_maintainer_scripts(package, release_map, scripts)
    scripts["postinst"] = (0o755, correct.replace("dkc-linux-headers-", "wrong-", 1))
    with pytest.raises(SystemExit, match="unexpected documentation symlink transition"):
        audit.validate_maintainer_scripts(package, release_map, scripts)


@pytest.mark.parametrize(
    ("relation", "message"),
    (("enhances", "unknown DKC package"), ("provides", "internal Provides differs")),
)
def test_unreviewed_internal_relation_is_rejected(relation: str, message: str) -> None:
    identity = _identity()
    abi = str(identity["abi"])
    releases = identity["kernel_releases"]
    assert isinstance(releases, dict)
    release_map = {str(key): str(value) for key, value in releases.items()}
    graph = audit.expected_internal_dependencies(abi, release_map)
    version = str(identity["package_version"])
    rows = {
        package: {field: "" for field in audit.RELATION_FIELDS}
        for package in graph
    }
    for package, dependencies in graph.items():
        rows[package]["depends"] = ", ".join(
            f"{related} (= {version})" if exact else related
            for related, exact in dependencies.items()
        )
    for package, provides in audit.expected_internal_provides(release_map).items():
        rows[package]["provides"] = ", ".join(sorted(provides))
    package = next(iter(graph))
    rows[package][relation] = "dkc-linux-unreviewed-virtual"
    with pytest.raises(SystemExit, match=message):
        audit.validate_internal_dependency_graph(abi, version, release_map, rows)


def test_cross_package_payload_collision_is_rejected(tmp_path: pathlib.Path) -> None:
    roots = _build_synthetic_matrix(tmp_path, duplicate_payload=True)
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "payload paths collide" in result.stderr


def test_unreviewed_package_role_payload_is_rejected(tmp_path: pathlib.Path) -> None:
    roots = _build_synthetic_matrix(tmp_path, unreviewed_payload=True)
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "owns an unreviewed payload path" in result.stderr


def test_renamed_kbuild_payload_breaking_header_abi_is_rejected(
    tmp_path: pathlib.Path,
) -> None:
    roots = _build_synthetic_matrix(tmp_path, renamed_kbuild_payload=True)
    result = _run_audit(tmp_path, roots)
    assert result.returncode != 0
    assert "renamed package name leaked into the conventional Kbuild" in result.stderr


def test_copyright_documentation_rejects_missing_target_and_cycles() -> None:
    record = tuple[str, str, int, int, str]
    missing: dict[str, dict[str, record]] = {
        "one": {"usr/share/doc/one": ("usr/share/doc/one", "symlink", 0o777, 0, "two")}
    }
    with pytest.raises(SystemExit, match="does not name a packaged documentation root"):
        audit.validate_copyright_documentation(missing)

    cycle: dict[str, dict[str, record]] = {
        "one": {"usr/share/doc/one": ("usr/share/doc/one", "symlink", 0o777, 0, "two")},
        "two": {"usr/share/doc/two": ("usr/share/doc/two", "symlink", 0o777, 0, "one")},
    }
    with pytest.raises(SystemExit, match="documentation symlink cycle"):
        audit.validate_copyright_documentation(cycle)


def test_copyright_documentation_rejects_missing_regular_file() -> None:
    payloads = {
        "one": {
            "usr/share/doc/one": ("usr/share/doc/one", "directory", 0o755, 0, "")
        }
    }
    with pytest.raises(SystemExit, match="regular copyright file"):
        audit.validate_copyright_documentation(payloads)


def test_unsafe_tar_path_and_link_are_rejected() -> None:
    member = tarfile.TarInfo("../escape")
    member.type = tarfile.REGTYPE
    with pytest.raises(SystemExit, match="unsafe package payload path"):
        audit.normalized_payload_member(member, "unsafe.deb")

    link = tarfile.TarInfo("usr/lib/modules/release/build")
    link.type = tarfile.SYMTYPE
    link.linkname = "../../../../../outside"
    with pytest.raises(SystemExit, match="escaping symlink target"):
        audit.normalized_payload_member(link, "unsafe.deb")

    setuid = tarfile.TarInfo("usr/bin/unsafe")
    setuid.type = tarfile.REGTYPE
    setuid.mode = 0o4755
    with pytest.raises(SystemExit, match="special permission bits"):
        audit.normalized_payload_member(setuid, "unsafe.deb")

    sticky_directory = tarfile.TarInfo("usr/share/unsafe")
    sticky_directory.type = tarfile.DIRTYPE
    sticky_directory.mode = 0o1755
    with pytest.raises(SystemExit, match="special permission bits"):
        audit.normalized_payload_member(sticky_directory, "unsafe.deb")
