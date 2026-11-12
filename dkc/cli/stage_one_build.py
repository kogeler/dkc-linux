"""Write the authenticated source inventory for stage-one-build.sh."""

from __future__ import annotations

import json
import pathlib
import sys


def main() -> None:
    arguments = sys.argv[1:]
    (
        root,
        version,
        llvm_major,
        dsc_url,
        dsc_name,
        dsc_sha,
        dsc_size,
        orig_url,
        orig_name,
        orig_sha,
        orig_size,
        debian_url,
        debian_name,
        debian_sha,
        debian_size,
    ) = arguments
    epoch = int((pathlib.Path(root) / "source-date-epoch").read_text().strip())
    record = {
        "schema_version": 2,
        "source": "linux",
        "version": version,
        "source_date_epoch": epoch,
        "llvm_major": int(llvm_major),
        "files": [
            {
                "name": dsc_name,
                "url": dsc_url,
                "sha256": dsc_sha,
                "size": int(dsc_size),
            },
            {
                "name": orig_name,
                "url": orig_url,
                "sha256": orig_sha,
                "size": int(orig_size),
            },
            {
                "name": debian_name,
                "url": debian_url,
                "sha256": debian_sha,
                "size": int(debian_size),
            },
        ],
    }
    (pathlib.Path(root) / "source-inventory.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
