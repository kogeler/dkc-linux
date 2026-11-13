"""Print the publication fields that build-source-package.sh needs."""

from __future__ import annotations

import json
import pathlib
import re
import sys

from dkc.debver import DebianVersion


def main() -> None:
    arguments = sys.argv[1:]
    value = json.loads(pathlib.Path(arguments[0]).read_text(encoding="utf-8"))
    for field in ("package_version", "publication_source_date_epoch"):
        item = value.get(field)
        if not isinstance(item, (str, int)):
            raise SystemExit(f"publication identity lacks {field}")
        print(item)
    debian = value.get("debian_source_version")
    if not isinstance(debian, str):
        raise SystemExit("publication identity lacks debian_source_version")
    print(DebianVersion.parse(debian).upstream_release)
    inventory = json.loads(pathlib.Path(arguments[1]).read_text(encoding="utf-8"))
    if (
        inventory.get("schema_version") != 2
        or inventory.get("version") != debian
        or not isinstance(inventory.get("files"), list)
    ):
        raise SystemExit("source inventory differs from the publication identity")
    orig_names: list[str] = [
        item["name"]
        for item in inventory["files"]
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item["name"].endswith(".orig.tar.xz")
    ]
    if len(orig_names) != 1 or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._+~-]*\.orig\.tar\.xz", orig_names[0]
    ):
        raise SystemExit("source inventory does not identify one safe orig member")
    print(pathlib.PurePosixPath(orig_names[0]).name)


if __name__ == "__main__":
    main()
