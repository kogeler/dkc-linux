"""Packaging-overlay checks for verify-overlay.sh."""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
import tomllib

from dkc.builddeps import all_build_depends


def check_package_graph(arguments: list[str]) -> None:
    """Check the generated package graph and its compiler dependencies."""
    major = arguments[0]
    selected = set(os.environ["DKC_SELECTED_PACKAGES"].splitlines())
    paragraphs = (
        pathlib.Path("debian/control").read_text(encoding="utf-8").split("\n\n")
    )
    headers = []
    header_metas = []
    images = []
    packages = {}
    for paragraph in paragraphs:
        fields = {}
        current = None
        for line in paragraph.splitlines():
            if line[:1] in (" ", "\t") and current:
                fields[current] += " " + line.strip()
                continue
            key, sep, value = line.partition(":")
            if sep:
                current = key
                fields[key] = value.strip()
        package = fields.get("Package", "")
        if package in selected:
            if package in packages:
                raise SystemExit(f"duplicate generated binary package: {package}")
            packages[package] = fields
        if re.fullmatch(r"dkc-linux-headers-[0-9].*-amd64", package):
            headers.append((package, fields.get("Depends", "")))
        elif re.fullmatch(r"dkc-linux-headers-v[234]-amd64", package):
            header_metas.append((package, fields.get("Depends", "")))
        if re.fullmatch(r"dkc-linux-(?:image|modules|binary).*amd64", package):
            images.append((package, fields.get("Depends", "")))

    if not headers:
        raise SystemExit("no amd64 headers packages were generated")
    for package, depends in headers:
        missing = [
            tool
            for tool in ("clang", "lld", "llvm")
            if not re.search(rf"(?:^|, ){tool}-{re.escape(major)}(?:[, (]|$)", depends)
        ]
        if missing:
            raise SystemExit(f"{package} lacks client tool dependencies: {missing}")

    # A headers meta-package need not duplicate those dependencies, but it must
    # depend on the corresponding versioned headers package so the tools remain a
    # mandatory transitive dependency rather than a Suggests/Recommends accident.
    for package, depends in header_metas:
        suffix = package.removeprefix("dkc-linux-headers-")
        if not re.search(
            rf"(?:^|, )dkc-linux-headers-[0-9][^, ]*-{re.escape(suffix)}(?:[, (]|$)",
            depends,
        ):
            raise SystemExit(
                f"{package} does not require its versioned headers package"
            )

    for package, depends in images:
        leaked = [
            tool
            for tool in ("clang", "lld", "llvm")
            if re.search(rf"(?:^|, ){tool}-{re.escape(major)}(?:[, (]|$)", depends)
        ]
        if leaked:
            raise SystemExit(f"{package} unexpectedly depends on compilers: {leaked}")

    common_names = [
        name for name in packages if re.fullmatch(r"dkc-linux-headers-.+-common", name)
    ]
    kbuild_names = [
        name for name in packages if re.fullmatch(r"dkc-linux-kbuild-.+", name)
    ]
    if len(common_names) != 1 or len(kbuild_names) != 1:
        raise SystemExit("generated common headers/Kbuild package is not unique")
    common, kbuild = common_names[0], kbuild_names[0]
    abi = common.removeprefix("dkc-linux-headers-").removesuffix("-common")
    if kbuild != f"dkc-linux-kbuild-{abi}":
        raise SystemExit("common headers and Kbuild ABI identities differ")

    release_by_flavor = {}
    for name in packages:
        if match := re.fullmatch(r"dkc-linux-image-([0-9].+-(v[234])-amd64)", name):
            release_by_flavor[match.group(2)] = match.group(1)
    if set(release_by_flavor) != {"v2", "v3", "v4"}:
        raise SystemExit(
            f"cannot derive exact generated flavor releases: {release_by_flavor}"
        )

    expected_graph: dict[str, dict[str, str | None]] = {common: {}, kbuild: {}}
    expected_provides: dict[str, set[str]] = {}
    for flavor, release in release_by_flavor.items():
        base = f"dkc-linux-base-{release}"
        binary = f"dkc-linux-binary-{release}"
        modules = f"dkc-linux-modules-{release}"
        image = f"dkc-linux-image-{release}"
        versioned_headers = f"dkc-linux-headers-{release}"
        base_meta = f"dkc-linux-base-{flavor}-amd64"
        image_meta = f"dkc-linux-image-{flavor}-amd64"
        headers_meta = f"dkc-linux-headers-{flavor}-amd64"
        expected_graph.update(
            {
                base: {},
                binary: {base: "${binary:Version}"},
                modules: {base: "${binary:Version}"},
                image: {
                    base: "${binary:Version}",
                    binary: "${binary:Version}",
                    modules: "${binary:Version}",
                },
                versioned_headers: {
                    base: "${binary:Version}",
                    common: "${source:Version}",
                    kbuild: None,
                },
                base_meta: {base: "${binary:Version}"},
                image_meta: {
                    base_meta: "${binary:Version}",
                    image: "${binary:Version}",
                },
                headers_meta: {
                    base_meta: "${binary:Version}",
                    versioned_headers: "${binary:Version}",
                },
            }
        )
        expected_provides[image_meta] = {f"dkc-linux-latest-modules-{release}"}
    if set(packages) != set(expected_graph):
        raise SystemExit(
            "generated package names differ from the exact 26-package graph: "
            f"missing={sorted(set(expected_graph) - set(packages))}, "
            f"unexpected={sorted(set(packages) - set(expected_graph))}"
        )

    relation_pattern = re.compile(
        r"(?<![A-Za-z0-9+.-])(dkc-linux-[a-z0-9+.-]+)"
        r"(?:\s*\(([<>=]+)\s*([^)\s]+)\))?"
    )
    for package, fields in packages.items():
        actual = {}
        actual_provides = set()
        for field in (
            "Depends",
            "Pre-Depends",
            "Recommends",
            "Suggests",
            "Enhances",
            "Breaks",
            "Provides",
            "Conflicts",
            "Replaces",
        ):
            relation_groups = fields.get(field, "").split(",")
            if any("dkc-linux-" in group and "|" in group for group in relation_groups):
                raise SystemExit(
                    f"{package} has an alternative internal relation in {field}"
                )
            for match in relation_pattern.finditer(fields.get(field, "")):
                related, operator, version = match.groups()
                if field == "Provides":
                    if operator is not None or version is not None:
                        raise SystemExit(
                            f"{package} has versioned internal virtual Provides "
                            f"{related}"
                        )
                    if related in actual_provides:
                        raise SystemExit(
                            f"{package} duplicates internal Provides {related}"
                        )
                    actual_provides.add(related)
                    continue
                if related not in packages:
                    raise SystemExit(
                        f"{package} {field} names unknown DKC product {related}"
                    )
                if field != "Depends":
                    raise SystemExit(f"{package} relates to {related} through {field}")
                if related in actual:
                    raise SystemExit(
                        f"{package} duplicates internal dependency {related}"
                    )
                actual[related] = version if operator == "=" else None
        if actual != expected_graph[package]:
            raise SystemExit(
                f"{package} internal graph differs: "
                f"expected={expected_graph[package]}, actual={actual}"
            )
        if actual_provides != expected_provides.get(package, set()):
            raise SystemExit(
                f"{package} internal Provides differs: "
                f"expected={expected_provides.get(package, set())}, "
                f"actual={actual_provides}"
            )

    print(
        f"  every versioned amd64 headers package depends on clang/lld/llvm-{major}: ok"
    )
    print("  every amd64 headers meta-package requires its versioned package: ok")
    print("  image/module/binary packages carry no compiler dependency: ok")
    print(
        "  all 26 generated packages have the exact reviewed internal dependency "
        "graph: ok"
    )


def check_header_paths(arguments: list[str]) -> None:
    """Check that renamed packages keep the conventional headers paths."""
    rules = pathlib.Path("debian/rules.real").read_text(encoding="utf-8")
    required_rules = (
        "binary_kbuild: PREFIX_DIR = /usr/lib/linux-kbuild-$(ABINAME)",
        "\tdh_link $(PREFIX_DIR) /usr/src/linux-kbuild-$(ABINAME)",
        "binary_headers-common: BASE_DIR = "
        "/usr/src/linux-headers-$(ABINAME)-common$(LOCALVERSION)",
        "binary_headers: BASE_DIR = /usr/src/linux-headers-$(ABINAME)$(LOCALVERSION)",
    )
    for line in required_rules:
        if rules.count(line) != 1:
            raise SystemExit(f"missing exact conventional header rule: {line}")

    templates = {
        "headers.install.j2": "usr/src/linux-headers-{{abiname}}{{localversion}}",
        "headers.links.j2": "usr/src/linux-headers-{{abiname}}{{localversion}}",
        "headers.featureset.links.j2": (
            "usr/src/linux-headers-{{abiname}}-common{{localversion}}"
        ),
    }
    for name, expected in templates.items():
        text = pathlib.Path("debian/templates", name).read_text(encoding="utf-8")
        if "usr/src/{{package}}" in text or expected not in text:
            raise SystemExit(
                f"{name} still derives its payload path from the renamed package"
            )

    common_links = sorted(
        pathlib.Path("debian").glob("dkc-linux-headers-*-common.links")
    )
    if len(common_links) != 1:
        raise SystemExit("generated DKC common header link inventory is not unique")
    match = re.fullmatch(r"dkc-linux-headers-(.+)-common[.]links", common_links[0].name)
    if not match:
        raise SystemExit("cannot derive ABI from generated common header links")
    abi = match.group(1)
    common_text = common_links[0].read_text(encoding="utf-8")
    common_pairs = {tuple(line.split()) for line in common_text.splitlines()}
    for leaf in ("scripts", "tools"):
        link = (
            f"usr/lib/linux-kbuild-{abi}/{leaf}",
            f"usr/src/linux-headers-{abi}-common/{leaf}",
        )
        if link not in common_pairs:
            raise SystemExit(f"generated common header link is wrong: {link}")
    print(
        "  package names are DKC while header payload/link paths remain "
        "conventional: ok"
    )


def check_lifecycle_scripts(arguments: list[str]) -> None:
    """Check that lifecycle scripts hand off removal hooks exactly once."""
    contracts = {
        "binary.postrm.in": (
            'if [ "$1" = remove ]; then',
            "linux-run-hooks image postrm",
        ),
        "image.preinst.in": ("linux-run-hooks image preinst",),
        "image.postinst.in": (
            "linux-update-symlinks",
            "linux-run-hooks image postinst",
        ),
        "image.prerm.in": ("linux-check-removal", "linux-run-hooks image prerm"),
        "image.postrm.in": (
            "linux-update-symlinks remove",
            'case "$1" in',
            "remove|purge)",
            "linux-run-hooks image postrm",
        ),
        "modules.postinst.j2": ("depmod",),
        "modules.prerm.j2": ("modules.builtin",),
        "headers.postinst.in": ("linux-run-hooks headers postinst",),
    }
    for name, needles in contracts.items():
        text = pathlib.Path("debian/templates", name).read_text(encoding="utf-8")
        if "version=" not in text:
            raise SystemExit(f"{name} no longer binds lifecycle actions to a KREL")
        for needle in needles:
            if needle not in text:
                raise SystemExit(f"{name} lacks lifecycle command {needle!r}")

    image_postrm = pathlib.Path("debian/templates/image.postrm.in").read_text(
        encoding="utf-8"
    )
    remove_branch, other_actions = image_postrm.split("remove|purge)", 1)[1].split(
        "*)", 1
    )
    if "linux-run-hooks image postrm" in remove_branch:
        raise SystemExit("image.postrm.in runs bootloader hooks before vmlinuz removal")
    if other_actions.count("linux-run-hooks image postrm") != 1:
        raise SystemExit("image.postrm.in non-removal hook contract differs")

    binary_scripts = sorted(
        pathlib.Path("debian").glob("dkc-linux-binary-*.postrm.amd64")
    )
    image_scripts = [
        path
        for path in sorted(
            pathlib.Path("debian").glob("dkc-linux-image-*.postrm.amd64")
        )
        if "version=" in path.read_text(encoding="utf-8")
    ]
    if len(binary_scripts) != 3 or len(image_scripts) != 3:
        raise SystemExit(
            "generated flavor lifecycle scripts differ: "
            f"binary={len(binary_scripts)} image={len(image_scripts)}"
        )
    for path in binary_scripts:
        text = path.read_text(encoding="utf-8")
        if (
            text.count("linux-run-hooks image postrm") != 1
            or 'if [ "$1" = remove ]; then' not in text
        ):
            raise SystemExit(f"{path.name} does not own the final removal hook")
    for path in image_scripts:
        text = path.read_text(encoding="utf-8")
        remove_branch, other_actions = text.split("remove|purge)", 1)[1].split("*)", 1)
        if "linux-run-hooks image postrm" in remove_branch:
            raise SystemExit(
                f"{path.name} runs hooks before its binary dependency is removed"
            )
        if other_actions.count("linux-run-hooks image postrm") != 1:
            raise SystemExit(f"{path.name} non-removal hook contract differs")
    print(
        "  generated image and binary lifecycle scripts hand off removal hooks "
        "exactly once: ok"
    )


def check_flavor_policy(arguments: list[str]) -> None:
    """Check the Debian flavor inventory and the DKC package policy."""
    root = pathlib.Path("debian/config/amd64")
    defines = tomllib.loads((root / "defines.toml").read_text(encoding="utf-8"))
    actual = [item["name"] for item in defines["flavour"]]
    expected = ["v2-amd64", "v3-amd64", "v4-amd64"]
    if actual != expected:
        raise SystemExit(f"unexpected Debian flavor inventory: {actual!r}")
    for flavor in expected:
        path = root / f"config.{flavor}"
        if not path.is_file():
            raise SystemExit(f"missing flavor config {path}")
    print("  Debian flavor inventory is exactly v2/v3/v4: ok")
    packages = tomllib.loads(pathlib.Path("debian/config/defines.toml").read_text())[
        "packages"
    ]
    expected_packages = {
        "docs": False,
        "installer": False,
        "libc_dev": False,
        "meta": True,
        "source": False,
        "tools_unversioned": False,
        "tools_versioned": True,
    }
    if packages != expected_packages:
        raise SystemExit(f"unexpected DKC package policy: {packages!r}")
    print("  DKC package source-of-truth excludes non-products and enables metas: ok")


def report_build_dependencies(arguments: list[str]) -> None:
    """Report build dependencies that no installed package or Provides satisfies."""
    control = pathlib.Path(arguments[0]).read_text(encoding="utf-8")
    profiles = frozenset(arguments[1].split())
    declared = all_build_depends(control, "amd64", profiles)
    without_profiles = all_build_depends(control, "amd64", frozenset())

    # Virtual packages count as installed: debhelper-compat is a Provides of
    # debhelper, and reporting it missing would be a false alarm.
    query = subprocess.run(
        ["dpkg-query", "-W", "-f", "${binary:Package}\t${Provides}\n"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    installed = set()
    for line in query.splitlines():
        name, _, provides = line.partition("\t")
        installed.add(name.split(":")[0])
        for item in provides.split(","):
            item = item.strip().split()[0] if item.strip() else ""
            if item:
                installed.add(item)

    missing = sorted(name for name in declared if name not in installed)
    dropped = sorted(set(without_profiles) - set(declared))
    print(
        f"  declared dependencies: {len(declared)} "
        f"(without profiles: {len(without_profiles)})"
    )
    print(f"  unresolved package names after Provides: {len(missing)}")
    for name in missing:
        print(f"    {name}")
    print(f"  dependencies dropped by profiles: {len(dropped)}")
    for name in dropped:
        print(f"    {name}")


def prepend_test_changelog(arguments: list[str]) -> None:
    """Prepend a throwaway changelog entry in the DKC version namespace."""
    path = pathlib.Path("debian/changelog")
    entry = (
        f"dkc-linux ({arguments[0]}+dkc13.1) trixie; urgency=medium\n\n"
        "  * Verify the derived package release namespace.\n\n"
        " -- DKC Build Service <build@dkc.invalid>  Sat, 08 Aug 2026 00:00:00 +0000\n\n"
    )
    path.write_text(entry + path.read_text(encoding="utf-8"), encoding="utf-8")


COMMANDS = {
    "package-graph": check_package_graph,
    "header-paths": check_header_paths,
    "lifecycle-scripts": check_lifecycle_scripts,
    "flavor-policy": check_flavor_policy,
    "build-dependencies": report_build_dependencies,
    "prepend-test-changelog": prepend_test_changelog,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        commands = "|".join(COMMANDS)
        raise SystemExit(f"usage: python3 -m dkc.cli.verify_overlay {{{commands}}} ...")
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()
