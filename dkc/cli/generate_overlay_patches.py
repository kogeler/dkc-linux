"""Generate or apply the DKC packaging overlay against a Debian kernel source.

Runs inside the build container:

    python3 -m dkc.cli.generate_overlay_patches <source-root> <output-dir>
        writes one file per non-empty patch;
    python3 -m dkc.cli.generate_overlay_patches \
        --apply <source-root> <llvm-major> <patch-dir>
        edits the tree in place after checking the committed patches.

The reviewed edits are data in `debian-overlay/edits.py`; `dkc.overlay` is the
engine that applies them.
"""

from __future__ import annotations

import pathlib
import sys

from dkc.overlay import apply, generate, load_overlay

USAGE = (
    "usage: python3 -m dkc.cli.generate_overlay_patches <source-root> <output-dir>\n"
    "       python3 -m dkc.cli.generate_overlay_patches --apply "
    "<source-root> <llvm-major> <patch-dir>"
)


def main() -> int:
    arguments = sys.argv[1:]
    applying = arguments[:1] == ["--apply"]
    if applying:
        arguments = arguments[1:]
    if len(arguments) != (3 if applying else 2):
        print(USAGE, file=sys.stderr)
        return 2
    root = pathlib.Path(arguments[0])
    overlay = load_overlay()
    if applying:
        llvm_major = int(arguments[1])
        reviewed = pathlib.Path(arguments[2])
        if not reviewed.is_dir():
            print("committed patch directory does not exist", file=sys.stderr)
            return 2
        apply(root, llvm_major, reviewed, overlay)
        return 0
    output = pathlib.Path(arguments[1])
    if not output.is_dir() or any(output.iterdir()):
        print("output directory must exist and be empty", file=sys.stderr)
        return 2

    for name, patch in generate(root, overlay).items():
        (output / name).write_text(patch)
        print(f"generated {name} ({len(patch.splitlines())} lines)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
