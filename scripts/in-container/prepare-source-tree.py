#!/usr/bin/env python3
"""Materialize the self-contained downstream portion of a final source tree."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import sys
import tomllib


README_MARKER = "DKC downstream rebuild"


EXPECTED_ARCHITECTURES = {
    "alpha": ("alpha",),
    "arc": ("arc",),
    "arm": ("armel", "armhf"),
    "arm64": ("arm64",),
    "parisc": ("hppa",),
    "loongarch": ("loong64",),
    "m68k": ("m68k",),
    "mips": (
        "mips",
        "mips64",
        "mips64r6",
        "mips64el",
        "mips64r6el",
        "mipsel",
        "mipsn32",
        "mipsn32el",
        "mipsn32r6",
        "mipsn32r6el",
        "mipsr6",
        "mipsr6el",
    ),
    "powerpc": ("powerpc", "ppc64", "ppc64el"),
    "riscv": ("riscv64",),
    "s390": ("s390x",),
    "sh": ("sh4",),
    "sparc": ("sparc64",),
    "x86": ("amd64", "i386", "x32"),
}


def normalize_public_modes(root: pathlib.Path) -> None:
    """Make source modes independent of the private container umask."""

    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = pathlib.Path(directory)
        for name in dirnames:
            path = directory_path / name
            if not path.is_symlink():
                path.chmod(0o755)
        for name in filenames:
            path = directory_path / name
            if path.is_symlink():
                continue
            if not path.is_file():
                raise SystemExit(
                    f"source tree contains a special file: {path.relative_to(root)}"
                )
            executable = path.stat().st_mode & 0o111
            path.chmod(0o755 if executable else 0o644)


def restrict_source_architectures(source: pathlib.Path, repo: pathlib.Path) -> None:
    definitions = tomllib.loads(
        (source / "debian/config/defines.toml").read_text(encoding="utf-8")
    )
    actual: dict[str, tuple[str, ...]] = {}
    for kernel_architecture in definitions.get("kernelarch", []):
        name = kernel_architecture.get("name")
        architectures = tuple(
            item.get("name")
            for item in kernel_architecture.get("debianarch", [])
        )
        if not isinstance(name, str) or not all(
            isinstance(item, str) for item in architectures
        ):
            raise SystemExit("Debian architecture inventory is malformed")
        if name in actual:
            raise SystemExit(f"duplicate Debian kernel architecture: {name}")
        actual[name] = architectures
    if actual != EXPECTED_ARCHITECTURES:
        raise SystemExit(
            "Debian architecture inventory changed; review the amd64-only source policy"
        )

    local = source / "debian/config.local/defines.toml"
    if local.exists():
        raise SystemExit("source unexpectedly contains debian/config.local/defines.toml")
    local.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo / "debian-overlay/source/amd64-only-defines.toml", local)


def extend_copyright(path: pathlib.Path, repo: pathlib.Path) -> None:
    from dkc.buildpolicy import build_policy_paths  # noqa: PLC0415

    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        start = lines.index("Files: debian/*")
    except ValueError as exc:
        raise SystemExit("Debian copyright lacks the expected debian/* stanza") from exc
    if start + 2 >= len(lines) or not lines[start + 1].startswith("Copyright: "):
        raise SystemExit("Debian copyright has a malformed debian/* stanza")
    end = start + 2
    while end < len(lines) and lines[end].startswith(" "):
        end += 1
    if end >= len(lines) or lines[end] != "License: GPL-2":
        raise SystemExit("Debian copyright changed the debian/* license")
    notice = "           2026 kogeler"
    if notice in lines[start:end]:
        raise SystemExit("downstream copyright notice was already injected")
    lines.insert(end, notice)

    mit_paths = [
        "debian/dkc/build-profiles",
        "debian/dkc/prepare-flavor.py",
    ]
    for source in build_policy_paths(repo):
        relative = source.relative_to(repo).as_posix()
        if relative.startswith("debian-overlay/") or relative == (
            "scripts/in-container/generate-overlay-patches.py"
        ):
            continue
        mit_paths.append(f"debian/dkc/build-inputs/{relative}")
    mit_stanza = [
        f"Files: {mit_paths[0]}",
        *[f" {item}" for item in mit_paths[1:]],
        "Copyright: 2026 kogeler",
        "License: MIT",
        " Permission is hereby granted, free of charge, to any person obtaining a copy",
        ' of this software and associated documentation files (the "Software"), to deal',
        " in the Software without restriction, including without limitation the rights",
        " to use, copy, modify, merge, publish, distribute, sublicense, and/or sell",
        " copies of the Software, and to permit persons to whom the Software is",
        " furnished to do so, subject to the following conditions:",
        " .",
        " The above copyright notice and this permission notice shall be included in all",
        " copies or substantial portions of the Software.",
        " .",
        ' THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR',
        " IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,",
        " FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE",
        " AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER",
        " LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,",
        " OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE",
        " SOFTWARE.",
        "",
    ]
    lines[start:start] = mit_stanza
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def copy_policy_inputs(repo: pathlib.Path, destination: pathlib.Path) -> str:
    sys.path.insert(0, str(repo))
    from dkc.buildpolicy import build_policy_digest, build_policy_paths  # noqa: PLC0415

    digest = build_policy_digest(repo)
    for source in build_policy_paths(repo):
        relative = source.relative_to(repo)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o755 if source.stat().st_mode & 0o111 else 0o644)
    return digest


def manifest(root: pathlib.Path) -> str:
    records: list[str] = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        records.append(f"{digest}  {path.relative_to(root).as_posix()}")
    return "\n".join(records) + "\n"


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--normalize-public-modes":
        normalize_public_modes(pathlib.Path(sys.argv[2]).resolve())
        return 0
    if len(sys.argv) != 4:
        print(
            "usage: prepare-source-tree.py <source-root> <repo-root> <inputs-root>\n"
            "       prepare-source-tree.py --normalize-public-modes <source-root>",
            file=sys.stderr,
        )
        return 2
    source, repo, inputs = (pathlib.Path(value).resolve() for value in sys.argv[1:])
    identity_path = inputs / "publication-identity.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if identity.get("source_package") != "dkc-linux":
        raise SystemExit("publication identity does not describe dkc-linux")
    package_version = identity.get("package_version")
    publication_epoch = identity.get("publication_source_date_epoch")
    lto_mode = identity.get("lto_mode")
    build_inputs = identity.get("build_inputs")
    if (
        not isinstance(package_version, str)
        or not isinstance(publication_epoch, int)
        or publication_epoch < 1
        or lto_mode not in ("none", "thin", "full")
        or not isinstance(build_inputs, dict)
        or build_inputs.get("lto_mode") != lto_mode
    ):
        raise SystemExit("publication identity lacks source-package fields")

    dkc = source / "debian/dkc"
    if dkc.exists():
        raise SystemExit("source tree already contains debian/dkc")
    build_policy = dkc / "build-inputs"
    build_policy.mkdir(parents=True)
    restrict_source_architectures(source, repo)
    actual_policy_digest = copy_policy_inputs(repo, build_policy)
    if build_inputs.get("overlay_sha256") != actual_policy_digest:
        raise SystemExit("embedded build-policy inputs differ from the publication identity")

    shutil.copyfile(identity_path, dkc / "publication-identity.json")
    shutil.copyfile(inputs / "build-image-debs.tsv", dkc / "build-image-debs.tsv")
    for flavor in ("v2", "v3", "v4"):
        shutil.copyfile(
            inputs / f"policy-config-{flavor}.json",
            dkc / f"policy-config-{flavor}.json",
        )
    shutil.copyfile(repo / "config/build-profiles", dkc / "build-profiles")
    shutil.copyfile(repo / "scripts/in-container/prepare-flavor.py", dkc / "prepare-flavor.py")
    shutil.copyfile(repo / "debian-overlay/source/rebuild-flavor", dkc / "rebuild-flavor")
    shutil.copyfile(repo / "debian-overlay/source/README.DKC", source / "debian/README.DKC")
    (dkc / "prepare-flavor.py").chmod(0o755)
    (dkc / "rebuild-flavor").chmod(0o755)

    (dkc / "MODIFICATIONS").write_text(
        "\n".join(
            (
                "DKC downstream modifications",
                "",
                f"Package version: {package_version}",
                "Copyright: 2026 kogeler",
                "License: GPL-2.0-only",
                "",
                "The downstream changes select an LLVM toolchain, preserve the",
                "kernel no-SIMD contract while adding x86-64-v2/v3/v4 baselines,",
                "disable random module signing, and place binary packages in an",
                "independent namespace. The exact reviewed inputs and dependency",
                "lock are stored in this directory.",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    readme = source / "debian/README.source"
    original = readme.read_text(encoding="utf-8")
    if README_MARKER in original:
        raise SystemExit("Debian README.source was already extended")
    readme.write_text(
        original.rstrip()
        + "\n\n"
        + README_MARKER
        + "\n"
        + "======================\n\n"
        + "The final source contains all three downstream flavor definitions. "
        + "See debian/README.DKC for the source-only rebuild procedure and "
        + "debian/dkc/publication-identity.json for the exact common identity.\n",
        encoding="utf-8",
    )
    extend_copyright(source / "debian/copyright", repo)

    (dkc / "embedded-inputs.sha256").write_text(
        manifest(dkc / "build-inputs"), encoding="utf-8"
    )
    normalize_public_modes(source)
    normalized = [
        source / "debian/changelog",
        source / "debian/copyright",
        source / "debian/README.source",
        source / "debian/README.DKC",
        *[path for path in dkc.rglob("*") if path.is_file()],
    ]
    for path in normalized:
        os.utime(path, (publication_epoch, publication_epoch), follow_symlinks=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
