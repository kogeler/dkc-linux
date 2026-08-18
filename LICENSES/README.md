# License scope

This repository combines independently written automation with patches derived
from the Linux kernel and Debian kernel packaging. A single blanket license
would misdescribe those materials, so licenses are assigned by path.

The root `LICENSE` applies under the MIT License to the independently written
project material, including:

- `.github/`, `config/`, `container/`, `dkc/`, `docs/`, `mk/`, `schemas/`, and
  `scripts/`, except for the path listed below;
- the top-level makefile and documentation;
- independently written tests and test fixtures.

The following material is distributed under GPL-2.0-only because it contains
or produces modifications derived from GPL-2.0-only Linux or GPL-2.0 Debian
kernel packaging:

- `debian-overlay/patches/`;
- `debian-overlay/source/`;
- `tests/integration/kselftest-patches/`;
- `scripts/in-container/generate-overlay-patches.py`.

The full GPL version 2 text is in `LICENSES/GPL-2.0-only.txt`.

Generated Debian source packages preserve the upstream Linux license files and
Debian `debian/copyright`. Their downstream `debian/copyright` also records the
copyright and license of the packaging modifications. Generated evidence and
archive metadata state facts about a build; including them in the repository
does not alter the licenses of the source or packages they describe.

Linux is a registered trademark of Linus Torvalds in the United States and
other countries. Debian is a registered trademark owned by Software in the
Public Interest, Inc. This project is independent and is not produced,
sponsored, endorsed, or affiliated with the Debian Project, Linus Torvalds, or
the Linux Foundation.
