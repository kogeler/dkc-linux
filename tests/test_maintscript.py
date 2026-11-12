"""Source-declared migration templates stay data and bind exact package identity."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from dkc.maintscript import load_metapackage_templates, metapackage_transition

PACKAGE = "dkc-linux-image-v2-amd64"
KREL = "7.3.3+dkc13.r3.g0123456789ab-v2-amd64"
TEMPLATE = (
    "dir_to_symlink /usr/share/bug/{{package}} "
    "dkc-linux-base-{{abiname}}{{localversion}} 7.3-1~exp1 {{package}}\n"
)


@pytest.mark.parametrize("cutoff", ["7.3-1~exp1", "99.42-1~exp7"])
def test_jinja_migration_uses_the_source_cutoff_without_a_version_branch(
    cutoff: str,
) -> None:
    assert metapackage_transition(
        TEMPLATE.replace("7.3-1~exp1", cutoff), PACKAGE, KREL
    ) == [
        "dpkg-maintscript-helper",
        "dir_to_symlink",
        f"/usr/share/bug/{PACKAGE}",
        f"dkc-linux-base-{KREL}",
        cutoff,
        PACKAGE,
        "--",
        "$@",
    ]


def test_legacy_documentation_migration_retains_its_exact_target() -> None:
    template = (
        "dir_to_symlink /usr/share/doc/@package@ "
        "dkc-linux-image-@abiname@@localversion@ 5.7~rc5-1~exp1 @package@\n"
    )
    arguments = metapackage_transition(template, PACKAGE, KREL)
    assert arguments[2:5] == [
        f"/usr/share/doc/{PACKAGE}",
        f"dkc-linux-image-{KREL}",
        "5.7~rc5-1~exp1",
    ]


def test_kernel_identity_cannot_introduce_shell_expressions() -> None:
    with pytest.raises(ValueError, match="different kernel identity"):
        metapackage_transition(TEMPLATE, PACKAGE, KREL.replace("7.3.3", "$(id)"))


@pytest.mark.parametrize(
    "template",
    [
        TEMPLATE + "rm /unrelated\n",
        TEMPLATE.replace("dir_to_symlink", "rm_conffile"),
        TEMPLATE.replace("/usr/share/bug/{{package}}", "/etc/unrelated"),
        TEMPLATE.replace("dkc-linux-base-", "linux-base-"),
        TEMPLATE.replace("{{abiname}}", "{{unknown}}"),
        TEMPLATE.replace("{{package}}", "{{package.upper()}}"),
        TEMPLATE.replace("7.3-1~exp1", "not-a-version"),
        TEMPLATE.replace("7.3-1~exp1 {{package}}", "7.3-1~exp1 unrelated"),
    ],
)
def test_unreviewed_commands_paths_and_expressions_are_rejected(template: str) -> None:
    with pytest.raises(ValueError):
        metapackage_transition(template, PACKAGE, KREL)


@pytest.mark.parametrize("extra", ["duplicate", "both-formats", "link", "oversized"])
def test_source_template_reader_rejects_ambiguous_or_non_plain_members(
    tmp_path: Path,
    extra: str,
) -> None:
    path = tmp_path / "debian.tar.xz"
    with tarfile.open(path, "w:xz") as archive:
        for role in ("image", "headers"):
            member = tarfile.TarInfo(f"debian/templates/{role}.meta.maintscript.j2")
            data = TEMPLATE.encode()
            if role == "image" and extra == "link":
                member.type = tarfile.SYMTYPE
                member.linkname = "/unrelated"
                archive.addfile(member)
                continue
            if role == "image" and extra == "oversized":
                data = b"x" * 4097
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
            if role != "image" or extra not in {"duplicate", "both-formats"}:
                continue
            member = tarfile.TarInfo(
                "debian/templates/image.meta.maintscript."
                + ("in" if extra == "both-formats" else "j2")
            )
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    with pytest.raises(ValueError):
        load_metapackage_templates(path)
