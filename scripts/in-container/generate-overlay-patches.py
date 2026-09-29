#!/usr/bin/env python3
"""Generate the DKC packaging overlay against a given Debian kernel source.

The overlay is defined here as exact anchored edits, and the `.patch` files
under `debian-overlay/patches/<source-profile>/` are its output. Both are
committed: the patches are what a reviewer reads, this file is what regenerates
them for every supported Debian packaging generation.

That matters because the anchors are the revalidation trigger. If Debian changes
one of these lines, generation fails loudly with the anchor that no longer
matches, instead of a patch applying with fuzz into something subtly different.

An edit that Debian spells differently in different packaging generations is a
`OneOf` of reviewed spellings. Exactly one spelling must match; none or several
is an error. `Absent` names a generation in which the edit has nothing to do.
Keeping every reviewed spelling lets one generator regenerate the overlay of an
older kernel series after a newer one has been added.

Patches form an ordered series and each one is generated against the result of
the previous ones, exactly as `patch` applies them.

An anchor holds only the lines an edit changes, plus the fewest unchanged lines
that place an insertion. Neighbouring text is not reviewed policy: upstream
stable updates routinely add lines next to an edit, and an anchor that spans
them turns every such update into a failed build. A value Debian changes on its
own schedule that the overlay does not depend on, such as the GCC version it
selects, is a `LinePattern` over the reviewed shape of its line.

Builds therefore apply these anchored edits (`--apply`) instead of replaying the
committed patches, whose three lines of context would reject any neighbouring
change. `--apply` requires every committed patch to describe exactly the lines
the generator changes in the tree being built. Context and hunk positions may
differ from the reviewed source; a different changed line may not, so a stale
patch or a different Debian spelling still stops the build.

Runs inside the build container:

    generate-overlay-patches.py <source-root> <llvm-major> <output-dir>
        writes one file per non-empty patch;
    generate-overlay-patches.py --apply <source-root> <llvm-major> <patch-dir>
        edits the tree in place after checking the committed patches.
"""

from __future__ import annotations

import difflib
import pathlib
import re
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Absent:
    """A generation in which `marker` does not exist, so the edit is a no-op."""

    marker: str


class OneOf:
    """Reviewed spellings of one edit across Debian packaging generations."""

    def __init__(self, *variants: tuple[str, str] | Absent) -> None:
        if len(variants) < 2:
            raise ValueError("OneOf needs at least two reviewed spellings")
        self.variants = variants


@dataclass(frozen=True)
class LinePattern:
    """One whole line whose reviewed shape, not its exact value, is the anchor.

    For a value Debian changes on its own schedule that the overlay does not
    depend on. `regex` must match exactly one complete line; `@LINE@` in the
    replacement stands for that line, including its newline.
    """

    regex: str
    replacement: str


class FileVariants:
    """One logical file that Debian renamed between packaging generations."""

    def __init__(self, *groups: tuple[str, list]) -> None:
        if len(groups) < 2:
            raise ValueError("FileVariants needs at least two reviewed file names")
        self.groups = groups


# --------------------------------------------------------------------------
# Patch 0: keep the Debian 13 kernel image layout
# --------------------------------------------------------------------------

# Debian 7.2 installs vmlinuz, config and System.map below the modules
# directory and relies on linux-base >= 4.17 hooks to copy them into /boot.
# Debian 13 ships linux-base 4.12, whose bootloader and initramfs hooks expect
# the files in /boot. Restoring the established layout keeps every Debian 13
# client, and every package test, on the same contract as older generations.
LAYOUT_GENCONTROL = (
    "debian/bin/gencontrol.py",
    [OneOf(
        (
            "        makeflags['IMAGE_FILE'] = config.build.kernel_file\n\n",
            "        makeflags['IMAGE_FILE'] = config.build.kernel_file\n"
            "        makeflags['IMAGE_INSTALL_STEM'] = config.build.kernel_stem\n\n",
        ),
        (
            "        makeflags['IMAGE_FILE'] = config.build.kernel_file\n"
            "        makeflags['IMAGE_INSTALL_STEM'] = config.build.kernel_stem\n",
            "        makeflags['IMAGE_FILE'] = config.build.kernel_file\n"
            "        makeflags['IMAGE_INSTALL_STEM'] = config.build.kernel_stem\n",
        ),
    )],
)

_BOOT_IMAGE_BLOCK = (
    "\tinstall -D -m644 '$(DIR)/$(IMAGE_FILE)' $(OUTPUT_DIR)/boot/$(IMAGE_INSTALL_STEM)-$(REAL_VERSION)\n"
    "ifeq ($(IMAGE_FILE),vmlinux)\n"
    "# This is the unprocessed ELF image, so we need to strip debug symbols\n"
    "\t$(CROSS_COMPILE)strip --strip-debug $(OUTPUT_DIR)/boot/$(IMAGE_INSTALL_STEM)-$(REAL_VERSION)\n"
    "endif\n"
    "\n"
    "\tsed '/CONFIG_\\(MODULE_SIG_\\(ALL\\|KEY\\)\\|SYSTEM_TRUSTED_KEYS\\|BUILD_SALT\\)[ =]/d' $(DIR)/.config \\\n"
    "\t\t> $(OUTPUT_DIR)/boot/config-$(REAL_VERSION)\n"
    "\techo \"ffffffffffffffff B The real System.map is in the linux-image-$(REAL_VERSION)-dbg package\" \\\n"
    "\t\t> $(OUTPUT_DIR)/boot/System.map-$(REAL_VERSION)\n"
    "\n"
    "\tinstall -D -m644 $(OUTPUT_DIR)/boot/$(IMAGE_INSTALL_STEM)-$(REAL_VERSION) $(OUTPUT_DIR)/lib/modules/$(REAL_VERSION)/vmlinuz.unsigned\n"
)

LAYOUT_RULES = (
    "debian/rules.real",
    [OneOf(
        (
            "\tinstall -D -m644 '$(DIR)/$(IMAGE_FILE)' $(OUTPUT_DIR_LIB)/vmlinuz\n"
            "ifeq ($(IMAGE_FILE),vmlinux)\n"
            "# This is the unprocessed ELF image, so we need to strip debug symbols\n"
            "\t$(CROSS_COMPILE)strip --strip-debug $(OUTPUT_DIR_LIB)/vmlinuz\n"
            "endif\n"
            "\n"
            "\tsed '/CONFIG_\\(MODULE_SIG_\\(ALL\\|KEY\\)\\|SYSTEM_TRUSTED_KEYS\\|BUILD_SALT\\)[ =]/d' $(DIR)/.config \\\n"
            "\t\t> $(OUTPUT_DIR_LIB)/config\n"
            "\techo \"ffffffffffffffff B The real System.map is in the linux-image-$(REAL_VERSION)-dbg package\" \\\n"
            "\t\t> $(OUTPUT_DIR_LIB)/System.map\n"
            "\n"
            "\tinstall -D -m644 $(OUTPUT_DIR_LIB)/vmlinuz $(OUTPUT_DIR_LIB)/vmlinuz.unsigned\n",
            "# Debian 13 linux-base has no hook that copies a kernel from its modules\n"
            "# directory, so keep the image, configuration and System.map in /boot.\n"
            + _BOOT_IMAGE_BLOCK,
        ),
        (_BOOT_IMAGE_BLOCK, _BOOT_IMAGE_BLOCK),
    )],
)

LAYOUT_TEMPLATES = [
    (
        "debian/templates/base.install.j2",
        [OneOf(
            (
                "lib/modules/{{abiname}}{{localversion}}/config                   usr/lib/modules/{{abiname}}{{localversion}}\n"
                "lib/modules/{{abiname}}{{localversion}}/System.map               usr/lib/modules/{{abiname}}{{localversion}}\n",
                "boot/config-*\nboot/System.map-*\n",
            ),
            ("boot/config-*\nboot/System.map-*\n", "boot/config-*\nboot/System.map-*\n"),
        )],
    ),
    (
        "debian/templates/binary.install.j2",
        [OneOf(
            (
                "lib/modules/{{abiname}}{{localversion}}/vmlinuz  usr/lib/modules/{{abiname}}{{localversion}}\n",
                "boot/vmlinu*-*\n",
            ),
            ("boot/vmlinu*-*\n", "boot/vmlinu*-*\n"),
        )],
    ),
    (
        "debian/templates/image.control.in",
        [OneOf(
            ("Pre-Depends: linux-base (>= 4.17~)\n", "Pre-Depends: linux-base (>= 4.12~)\n"),
            ("Pre-Depends: linux-base (>= 4.12~)\n", "Pre-Depends: linux-base (>= 4.12~)\n"),
        )],
    ),
]

# --------------------------------------------------------------------------
# Patch 1: select the LLVM toolchain in the generated dependencies
# --------------------------------------------------------------------------

CONFIG_SCHEMA = (
    "debian/lib/python/debian_linux/config_v2.py",
    [(
        "    c_compiler: Optional[str] = None\n",
        "    c_compiler: Optional[str] = None\n"
        "    llvm_major: Optional[int] = None\n"
        "    abi_name: Optional[str] = None\n",
    )],
)

# Debian moves the GCC it selects on its own schedule (gcc-15 in 7.1, gcc-16 in
# 7.2). The overlay replaces whichever one it is, and release-preflight fails if
# any generated relation still names it, so the version is not pinned here.
DEFINES = (
    "debian/config/defines.toml",
    [LinePattern(r"c_compiler = 'gcc-[0-9]+'", "@LINE@llvm_major = @LLVM_MAJOR@\n")],
)

GENCONTROL = (
    "debian/bin/gencontrol.py",
    [
        (
            "        makeflags['C_COMPILER'] = config.build.c_compiler\n",
            "        makeflags['C_COMPILER'] = config.build.c_compiler\n"
            "        if llvm_major := config.build.llvm_major:\n"
            "            makeflags['LLVM_MAJOR'] = str(llvm_major)\n",
        ),
        (
            """        relation_c_compiler = PackageRelationEntry(cast(str, config.build.c_compiler))
        relation_c_compiler_host = PackageRelationEntry(
            relation_c_compiler,
            name=f'{relation_c_compiler.name}-for-host',
        )

        # Generate compiler build-depends:
        self.bundle.source.build_depends_arch.merge([
            PackageRelationEntry(
                relation_c_compiler_host,
                arches={arch},
                restrictions='<!pkg.linux.nokernel>',
            )
        ])""",
            """        llvm_major = config.build.llvm_major
        relation_c_compiler = PackageRelationEntry(cast(str, config.build.c_compiler))
        if llvm_major:
            # LLVM ships no -for-host meta-package, so depend on the real
            # versioned package. Fabricating clang-N-for-host would generate a
            # dependency that can never be satisfied.
            relation_c_compiler_host = PackageRelationEntry(f'clang-{llvm_major}')
            relation_llvm_tools = [
                PackageRelationEntry(f'{tool}-{llvm_major}')
                for tool in ('clang', 'lld', 'llvm')
            ]
        else:
            relation_c_compiler_host = PackageRelationEntry(
                relation_c_compiler,
                name=f'{relation_c_compiler.name}-for-host',
            )

        # Generate compiler build-depends:
        if llvm_major:
            # One merge per package: merging a list would make them
            # alternatives, and apt would satisfy the whole group by installing
            # clang alone, leaving the build without a linker.
            for relation_llvm_tool in relation_llvm_tools:
                self.bundle.source.build_depends_arch.merge([
                    PackageRelationEntry(
                        relation_llvm_tool,
                        arches={arch},
                        restrictions='<!pkg.linux.nokernel>',
                    )
                ])
        else:
            self.bundle.source.build_depends_arch.merge([
                PackageRelationEntry(
                    relation_c_compiler_host,
                    arches={arch},
                    restrictions='<!pkg.linux.nokernel>',
                )
            ])""",
        ),
        (
            """        if gnutype := config.build.compiler_gnutype:
            if gnutype != config.defs_debianarch.gnutype:""",
            """        # A cross toolchain is named by GNU triplet; clang is never invoked
        # through a triplet prefix, so no such dependency is synthesised for it.
        if (gnutype := config.build.compiler_gnutype) and not llvm_major:
            if gnutype != config.defs_debianarch.gnutype:""",
        ),
        OneOf(
            (
                "        packages_headers[0].depends.merge([relation_c_compiler_host])\n",
                "        if llvm_major:\n"
                "            # A plain external-module build consumes every tool named in\n"
                "            # .kernelvariables.  clang alone does not install lld or the\n"
                "            # versioned llvm-ar/nm/objcopy tools, so the headers package\n"
                "            # must make the complete client-side closure installable.\n"
                "            for relation_llvm_tool in relation_llvm_tools:\n"
                "                packages_headers[0].depends.merge([relation_llvm_tool])\n"
                "        else:\n"
                "            packages_headers[0].depends.merge([relation_c_compiler_host])\n",
            ),
            (
                "        for p in packages_headers:\n"
                "            p.depends.merge([relation_c_compiler_host])\n",
                "        for p in packages_headers:\n"
                "            if llvm_major:\n"
                "                # A plain external-module build consumes every tool named in\n"
                "                # .kernelvariables.  clang alone does not install lld or the\n"
                "                # versioned llvm-ar/nm/objcopy tools, so the headers package\n"
                "                # must make the complete client-side closure installable.\n"
                "                for relation_llvm_tool in relation_llvm_tools:\n"
                "                    p.depends.merge([relation_llvm_tool])\n"
                "            else:\n"
                "                p.depends.merge([relation_c_compiler_host])\n",
            ),
        ),
        (
            "        else:\n"
            "            self.abiname = version.linux_version + self.debianrelease.abi_suffix\n"
            "\n"
            "        self.vars = {\n",
            "        else:\n"
            "            self.abiname = version.linux_version + self.debianrelease.abi_suffix\n"
            "\n"
            "        if configured_abiname := self.config.build.abi_name:\n"
            "            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.+~-]*', configured_abiname):\n"
            "                raise RuntimeError(f'Invalid configured ABI name: {configured_abiname!r}')\n"
            "            self.abiname = configured_abiname\n"
            "\n"
            "        self.vars = {\n",
        ),
    ],
)

# --------------------------------------------------------------------------
# Patch 2: actually drive Kbuild with that toolchain
# --------------------------------------------------------------------------

RULES_REAL = (
    "debian/rules.real",
    [
        (
            "MAKE_CLEAN = $(setup_env) $(MAKE) \\\n",
            "# LLVM=-<major> belongs on the command line, not in .kernelvariables:\n"
            "# the kernel binds CC, LD, AR and the rest from $(LLVM) near the top of\n"
            "# its Makefile, while Debian includes .kernelvariables about 130 lines\n"
            "# further down. A command-line variable is visible from the start of\n"
            "# parsing, and MAKE_CLEAN wraps every Kbuild entry point, so setting it\n"
            "# once here covers configuration, kernel, modules and headers alike.\n"
            "MAKE_CLEAN = $(setup_env) $(MAKE) \\\n"
            "\t$(if $(LLVM_MAJOR),LLVM=-$(LLVM_MAJOR)) \\\n",
        ),
        (
            "ifeq (./,$(dir $(C_COMPILER)))\n",
            "ifdef LLVM_MAJOR\n"
            "# .kernelvariables is included too late for LLVM= to select the toolchain,\n"
            "# so every tool is set by name. That is also what an out-of-tree build\n"
            "# needs: a plain `make -C /usr/lib/modules/<rel>/build M=$$PWD modules`\n"
            "# passes no LLVM= and would otherwise use GNU tools against a kernel\n"
            "# built with Clang. No GNU triplet is prepended, because\n"
            "# x86_64-linux-gnu-clang-N does not exist.\n"
            "@KERNELVARIABLES@"
            "else ifeq (./,$(dir $(C_COMPILER)))\n",
        ),
    ],
)

# --------------------------------------------------------------------------
# Patch 3: remove random module signing and use LLVM for packaging ELF actions
# --------------------------------------------------------------------------

AMD64_DEFINES = (
    "debian/config/amd64/defines.toml",
    [(
        "enable_signed = true\n",
        "# DKC's initial product has no Secure Boot trust chain. Build the\n"
        "# directly installable image package instead of Debian's separate\n"
        "# official signing stage.\n"
        "enable_signed = false\n",
    )],
)

# --------------------------------------------------------------------------
# Patch 4: real x86-64-v2/v3/v4 compiler baselines
# --------------------------------------------------------------------------

# The choice follows X86_NATIVE_CPU, which upstream keeps revising (Linux 7.2.8
# added a Rust APX dependency). Anchoring on the header of the next entry places
# the block at the same position without pinning the dependencies or help text
# of X86_NATIVE_CPU. The Makefile edit below still fails if the native/generic
# split itself changes.
KCONFIG_CPU = (
    "arch/x86/Kconfig.cpu",
    [(
        "config X86_GENERIC\n",
        "choice\n"
        "\tprompt \"DKC x86-64 compiler baseline\"\n"
        "\tdepends on X86_64 && !X86_NATIVE_CPU\n"
        "\tdefault DKC_X86_64_BASELINE_GENERIC\n"
        "\thelp\n"
        "\t  Select the psABI micro-architecture baseline for ordinary\n"
        "\t  64-bit kernel C and Rust code. The architecture Makefile still\n"
        "\t  disables implicit MMX/SSE/AVX generation; explicit kernel FPU\n"
        "\t  contexts remain controlled by their target-specific flags.\n"
        "\n"
        "config DKC_X86_64_BASELINE_GENERIC\n"
        "\tbool \"Generic x86-64\"\n"
        "\n"
        "config DKC_X86_64_BASELINE_V2\n"
        "\tbool \"x86-64-v2\"\n"
        "\n"
        "config DKC_X86_64_BASELINE_V3\n"
        "\tbool \"x86-64-v3\"\n"
        "\n"
        "config DKC_X86_64_BASELINE_V4\n"
        "\tbool \"x86-64-v4\"\n"
        "\n"
        "endchoice\n"
        "\n"
        "config X86_GENERIC\n",
    )],
)

# Each no-SIMD line is its own anchor, so the Rust target line between them is
# not pinned. The baseline edit anchors on the generic branch only: `else`
# proves it is the fallback of the native-CPU conditional, while the native
# branch, which upstream changed in Linux 7.2.8, stays outside the anchor.
X86_MAKEFILE = (
    "arch/x86/Makefile",
    [
        (
            "KBUILD_CFLAGS += -mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx -mno-sse4a\n",
            "X86_CFLAGS_NO_SIMD := -mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx -mno-sse4a\n"
            "X86_RUSTFLAGS_NO_SIMD := -Ctarget-feature=-sse,-sse2,-sse3,-ssse3,-sse4.1,-sse4.2,-avx,-avx2\n"
            "KBUILD_CFLAGS += $(X86_CFLAGS_NO_SIMD)\n",
        ),
        (
            "KBUILD_RUSTFLAGS += -Ctarget-feature=-sse,-sse2,-sse3,-ssse3,-sse4.1,-sse4.2,-avx,-avx2\n",
            "KBUILD_RUSTFLAGS += $(X86_RUSTFLAGS_NO_SIMD)\n",
        ),
        (
            "else\n"
            "        KBUILD_CFLAGS += -march=x86-64 -mtune=generic\n"
            "        KBUILD_RUSTFLAGS += -Ctarget-cpu=x86-64 -Ztune-cpu=generic\n"
            "endif\n",
            "else\n"
            "        DKC_X86_64_TARGET := x86-64\n"
            "ifeq ($(CONFIG_DKC_X86_64_BASELINE_V2),y)\n"
            "        DKC_X86_64_TARGET := x86-64-v2\n"
            "else ifeq ($(CONFIG_DKC_X86_64_BASELINE_V3),y)\n"
            "        DKC_X86_64_TARGET := x86-64-v3\n"
            "else ifeq ($(CONFIG_DKC_X86_64_BASELINE_V4),y)\n"
            "        DKC_X86_64_TARGET := x86-64-v4\n"
            "endif\n"
            "        KBUILD_CFLAGS += -march=$(DKC_X86_64_TARGET) -mtune=generic\n"
            "        KBUILD_RUSTFLAGS += -Ctarget-cpu=$(DKC_X86_64_TARGET) -Ztune-cpu=generic\n"
            "endif\n"
            "\n"
            "        # A psABI baseline implies SIMD features. Reassert the kernel's\n"
            "        # no-SIMD policy after -march/-Ctarget-cpu so compiler ordering\n"
            "        # cannot enable vector state in ordinary kernel code. Per-object\n"
            "        # CC_FLAGS_FPU are appended later by scripts/Makefile.lib.\n"
            "        KBUILD_CFLAGS += $(X86_CFLAGS_NO_SIMD)\n"
            "        KBUILD_RUSTFLAGS += $(X86_RUSTFLAGS_NO_SIMD)\n",
        ),
    ],
)

AMD64_FLAVOURS = (
    "debian/config/amd64/defines.toml",
    [(
        "[[flavour]]\n"
        "name = 'amd64'\n"
        "[flavour.defs]\n"
        "is_default = true\n"
        "[flavour.description]\n"
        "hardware = '64-bit PCs'\n"
        "hardware_long = 'PCs with AMD64, Intel 64 or VIA Nano processors'\n"
        "[flavour.packages]\n"
        "installer = true\n"
        "\n"
        "[[flavour]]\n"
        "name = 'cloud-amd64'\n"
        "[flavour.build]\n"
        "config = ['config.cloud']\n"
        "[flavour.description]\n"
        "hardware = 'x86-64 cloud'\n"
        "hardware_long = 'cloud platforms including Amazon EC2, Microsoft Azure, and Google Compute Engine'\n"
        "\n"
        "[[flavour]]\n"
        "name = 'rt-amd64'\n"
        "[flavour.build]\n"
        "config = ['config.rt']\n"
        "[flavour.description]\n"
        "hardware = '64-bit PCs'\n"
        "hardware_long = 'PCs with AMD64, Intel 64 or VIA Nano processors'\n"
        "parts = ['rt']\n"
        "\n"
        "[[flavour]]\n"
        "name = 'test'\n"
        "[flavour.build]\n"
        "config = ['config.test', 'amd64/config.test']\n"
        "[flavour.defs]\n"
        "is_test = true\n"
        "[flavour.description]\n"
        "hardware = \"CI only\"\n"
        "hardware_long = \"CI only\"\n"
        "[flavour.packages]\n"
        "installer = true\n",
        "[[flavour]]\n"
        "name = 'v2-amd64'\n"
        "[flavour.description]\n"
        "hardware = 'x86-64-v2 PCs'\n"
        "hardware_long = '64-bit PCs implementing the x86-64-v2 psABI level'\n"
        "\n"
        "[[flavour]]\n"
        "name = 'v3-amd64'\n"
        "[flavour.description]\n"
        "hardware = 'x86-64-v3 PCs'\n"
        "hardware_long = '64-bit PCs implementing the x86-64-v3 psABI level'\n"
        "\n"
        "[[flavour]]\n"
        "name = 'v4-amd64'\n"
        "[flavour.description]\n"
        "hardware = 'x86-64-v4 PCs'\n"
        "hardware_long = '64-bit PCs implementing the x86-64-v4 psABI level'\n",
    )],
)

FLAVOUR_CONFIGS = {
    "debian/config/amd64/config.v2-amd64": (
        "# DKC x86-64-v2 compiler baseline.\n"
        "# CONFIG_X86_NATIVE_CPU is not set\n"
        "CONFIG_DKC_X86_64_BASELINE_V2=y\n"
    ),
    "debian/config/amd64/config.v3-amd64": (
        "# DKC x86-64-v3 compiler baseline.\n"
        "# CONFIG_X86_NATIVE_CPU is not set\n"
        "CONFIG_DKC_X86_64_BASELINE_V3=y\n"
    ),
    "debian/config/amd64/config.v4-amd64": (
        "# DKC x86-64-v4 compiler baseline.\n"
        "# CONFIG_X86_NATIVE_CPU is not set\n"
        "CONFIG_DKC_X86_64_BASELINE_V4=y\n"
    ),
}

# --------------------------------------------------------------------------
# Patch 5: collision-free DKC package and ABI namespace
# --------------------------------------------------------------------------

DKC_GENCONTROL = (
    "debian/bin/gencontrol.py",
    [OneOf(
        (
            "        packages_own.extend(\n"
            "            self.bundle.add('image-dbg', ruleid, makeflags, vars, arch=arch)\n"
            "        )\n"
            "        if do_meta:\n"
            "            packages_own.extend(\n"
            "                bundle_signed.add('image-dbg.meta', ruleid, makeflags, vars, arch=arch)\n"
            "            )\n\n",
            "        # Detached debug packages are intentionally outside the product.\n"
            "        # Omitting their control stanzas also keeps the source package's\n"
            "        # declared binary graph identical to the published graph.\n\n",
        ),
        (
            "        packages_own.extend(\n"
            "            self.bundle.add('image-dbg', ruleid, makeflags, vars, arch=arch)\n"
            "        )\n"
            "\n"
            "        if do_meta:\n"
            "            packages_own.extend(bundle_signed.add('base.meta', ruleid, makeflags, vars, arch=arch))\n"
            "            packages_own.extend(bundle_signed.add('image.meta', ruleid, makeflags, vars, arch=arch))\n"
            "            packages_own.extend(\n"
            "                bundle_signed.add('headers.meta', ruleid, makeflags, vars, arch=arch))\n"
            "            packages_own.extend(\n"
            "                bundle_signed.add('image-dbg.meta', ruleid, makeflags, vars, arch=arch))\n",
            "        # Detached debug packages are intentionally outside the product.\n"
            "        # Omitting their control stanzas also keeps the source package's\n"
            "        # declared binary graph identical to the published graph.\n"
            "\n"
            "        if do_meta:\n"
            "            packages_own.extend(bundle_signed.add('base.meta', ruleid, makeflags, vars, arch=arch))\n"
            "            packages_own.extend(bundle_signed.add('image.meta', ruleid, makeflags, vars, arch=arch))\n"
            "            packages_own.extend(\n"
            "                bundle_signed.add('headers.meta', ruleid, makeflags, vars, arch=arch))\n",
        ),
    )],
)

DKC_DEBIAN_RELEASE = (
    "debian/config/defines.toml",
    [
        (
            "[[debianrelease]]\n"
            "name_regex = 'unstable'\n",
            "[[debianrelease]]\n"
            "name_regex = 'trixie'\n"
            "abi_suffix = '+dkc13'\n"
            "revision_regex = '\\d+(\\.\\d+)?\\+dkc13\\.\\d+'\n"
            "\n"
            "[[debianrelease]]\n"
            "name_regex = 'unstable'\n",
        ),
        (
            "[build]\n",
            "# DKC publishes the kernel, its headers, and the versioned Kbuild\n"
            "# support package.  Debian's docs, linux-source tarball, libc UAPI\n"
            "# headers, installer udebs, and unversioned tools are separate\n"
            "# products and must not leak into the DKC binary matrix.\n"
            "[packages]\n"
            "docs = false\n"
            "installer = false\n"
            "libc_dev = false\n"
            "meta = true\n"
            "source = false\n"
            "tools_unversioned = false\n"
            "tools_versioned = true\n"
            "\n"
            "[build]\n",
        ),
    ],
)

PACKAGE_TEMPLATES = [
    (
        "debian/templates/source.control.in",
        [
            (
                "Maintainer: Debian Kernel Team <debian-kernel@lists.debian.org>\n"
                "Uploaders: Bastian Blank <waldi@debian.org>, maximilian attems <maks@debian.org>, Ben Hutchings <benh@debian.org>, Salvatore Bonaccorso <carnil@debian.org>\n",
                "Maintainer: DKC Kernel Maintainers <build@dkc.invalid>\n",
            ),
            (
                "dh-python <!pkg.linux.notools>",
                "dh-python <!pkg.dkc.nokbuild>",
            ),
            (
                "Vcs-Git: https://salsa.debian.org/kernel-team/linux.git\n"
                "Vcs-Browser: https://salsa.debian.org/kernel-team/linux\n"
                "Homepage: https://www.kernel.org/\n",
                "Vcs-Git: https://github.com/kogeler/dkc-linux.git\n"
                "Vcs-Browser: https://github.com/kogeler/dkc-linux\n"
                "Homepage: https://github.com/kogeler/dkc-linux\n",
            ),
        ],
    ),
    (
        "debian/templates/base.control.in",
        [("Package: linux-base-@abiname@@localversion@\n", "Package: dkc-linux-base-@abiname@@localversion@\n")],
    ),
    (
        "debian/templates/base.meta.control.in",
        [
            ("Package: linux-base@source_suffix@@localversion@\n", "Package: dkc-linux-base@source_suffix@@localversion@\n"),
            ("INSTALLDOCS_LINK_DOC=linux-base-@abiname@@localversion@", "INSTALLDOCS_LINK_DOC=dkc-linux-base-@abiname@@localversion@"),
            (" linux-base-@abiname@@localversion@ (= ${binary:Version}),", " dkc-linux-base-@abiname@@localversion@ (= ${binary:Version}),"),
            (" linux-headers-@class@ and linux-image-@class@ synchronised.", " dkc-linux-headers-@class@ and dkc-linux-image-@class@ synchronised."),
        ],
    ),
    (
        "debian/templates/base.meta.lintian-overrides.j2",
        [(
            "# linux-signed-* source packages are generated by the linux source\n"
            "# package, so it is OK for their binaries to share documentation\n"
            "{{package}}: usr-share-doc-symlink-to-foreign-package linux-base-{{abiname}}{{localversion}}\n",
            "",
        )],
    ),
    (
        "debian/templates/binary.control.j2",
        [
            ("Package: linux-binary", "Package: dkc-linux-binary"),
            ("INSTALLDOCS_LINK_DOC=linux-base-", "INSTALLDOCS_LINK_DOC=dkc-linux-base-"),
            (" linux-base-{{abiname}}{{localversion}}", " dkc-linux-base-{{abiname}}{{localversion}}"),
            (" normally install linux-image-{{abiname}}{{localversion}}.", " normally install dkc-linux-image-{{abiname}}{{localversion}}."),
        ],
    ),
    (
        "debian/templates/binary.links.j2",
        [("usr/share/bug/linux-base-", "usr/share/bug/dkc-linux-base-")],
    ),
    (
        "debian/templates/image.postrm.in",
        [(
            """if command -v linux-run-hooks >/dev/null; then
    linux-run-hooks image postrm $version $image_path -- "$@"
else
    echo >&2 'W: linux-base is not installed; cannot run postrm hooks'
fi
""",
            """case "$1" in
remove|purge)
    # The binary package runs the removal hooks after dpkg has removed the
    # kernel image.  Running them here would leave a stale bootloader entry.
    ;;
*)
    if command -v linux-run-hooks >/dev/null; then
        linux-run-hooks image postrm $version $image_path -- "$@"
    else
        echo >&2 'W: linux-base is not installed; cannot run postrm hooks'
    fi
    ;;
esac
""",
        )],
    ),
    (
        "debian/templates/modules.control.in",
        [
            ("Package: linux-modules-", "Package: dkc-linux-modules-"),
            ("INSTALLDOCS_LINK_DOC=linux-base-", "INSTALLDOCS_LINK_DOC=dkc-linux-base-"),
            (" linux-base-@abiname@@localversion@", " dkc-linux-base-@abiname@@localversion@"),
            (" normally install linux-image-@abiname@@localversion@.", " normally install dkc-linux-image-@abiname@@localversion@."),
        ],
    ),
    (
        "debian/templates/image.control.in",
        [
            ("Package: linux-image-", "Package: dkc-linux-image-"),
            ("INSTALLDOCS_LINK_DOC=linux-base-", "INSTALLDOCS_LINK_DOC=dkc-linux-base-"),
            (" linux-base-@abiname@@localversion@", " dkc-linux-base-@abiname@@localversion@"),
            (" linux-binary-@abiname@@localversion@", " dkc-linux-binary-@abiname@@localversion@"),
            (" linux-modules-@abiname@@localversion@", " dkc-linux-modules-@abiname@@localversion@"),
        ],
    ),
    (
        "debian/templates/image.links.j2",
        [("usr/share/bug/linux-base-", "usr/share/bug/dkc-linux-base-")],
    ),
    (
        "debian/templates/image.lintian-overrides.j2",
        [(
            "# linux-signed-* source packages are generated by the linux source\n"
            "# package, so it is OK for their binaries to share documentation\n"
            "{{package}}: usr-share-doc-symlink-to-foreign-package linux-base-{{abiname}}{{localversion}}\n\n",
            "",
        )],
    ),
    FileVariants(
        (
            "debian/templates/image.meta.control.in",
            [
                ("Package: linux-image@source_suffix@@localversion@", "Package: dkc-linux-image@source_suffix@@localversion@"),
                ("INSTALLDOCS_LINK_DOC=linux-base@source_suffix@@localversion@", "INSTALLDOCS_LINK_DOC=dkc-linux-base@source_suffix@@localversion@"),
                (" linux-base@source_suffix@@localversion@", " dkc-linux-base@source_suffix@@localversion@"),
                (" linux-image-@abiname@@localversion@", " dkc-linux-image-@abiname@@localversion@"),
                ("linux-latest-modules-", "dkc-linux-latest-modules-"),
                (" (meta-package)", " (metapackage)"),
            ],
        ),
        (
            "debian/templates/image.meta.control.j2",
            [
                ("Package: linux-image{{source_suffix}}{{localversion}}", "Package: dkc-linux-image{{source_suffix}}{{localversion}}"),
                ("INSTALLDOCS_LINK_DOC=linux-base{{source_suffix}}{{localversion}}", "INSTALLDOCS_LINK_DOC=dkc-linux-base{{source_suffix}}{{localversion}}"),
                (" linux-base{{source_suffix}}{{localversion}}", " dkc-linux-base{{source_suffix}}{{localversion}}"),
                (" linux-image-{{abiname}}{{localversion}}", " dkc-linux-image-{{abiname}}{{localversion}}"),
                ("linux-latest-modules-", "dkc-linux-latest-modules-"),
                (" (meta-package)", " (metapackage)"),
            ],
        ),
    ),
    (
        "debian/templates/image.meta.bug-presubj.in",
        [("package name linux-image-", "package name dkc-linux-image-")],
    ),
    (
        "debian/templates/image.meta.lintian-overrides.j2",
        [(
            "# linux-signed-* source packages are generated by the linux source\n"
            "# package, so it is OK for their binaries to share documentation\n"
            "{{package}}: usr-share-doc-symlink-to-foreign-package linux-base-{{abiname}}{{localversion}}\n",
            "",
        )],
    ),
    (
        "debian/templates/image.meta.maintscript.in",
        [(" linux-image-@abiname@@localversion@ ", " dkc-linux-image-@abiname@@localversion@ ")],
    ),
    (
        "debian/templates/headers.control.in",
        [
            ("Package: linux-headers-", "Package: dkc-linux-headers-"),
            ("INSTALLDOCS_LINK_DOC=linux-base-", "INSTALLDOCS_LINK_DOC=dkc-linux-base-"),
            (" linux-base-@abiname@@localversion@", " dkc-linux-base-@abiname@@localversion@"),
            (" linux-headers-@abiname@-common@localversion_headers@", " dkc-linux-headers-@abiname@-common@localversion_headers@"),
            (" linux-kbuild-@abiname@", " dkc-linux-kbuild-@abiname@"),
            (" linux-image-@abiname@@localversion@ package.", " dkc-linux-image-@abiname@@localversion@ package."),
        ],
    ),
    (
        "debian/templates/headers.featureset.control.in",
        [
            ("Package: linux-headers-", "Package: dkc-linux-headers-"),
            (" linux-headers-@abiname@-(flavour) package", " dkc-linux-headers-@abiname@-(flavour) package"),
        ],
    ),
    FileVariants(
        (
            "debian/templates/headers.meta.control.in",
            [
                ("Package: linux-headers@source_suffix@@localversion@", "Package: dkc-linux-headers@source_suffix@@localversion@"),
                ("INSTALLDOCS_LINK_DOC=linux-base@source_suffix@@localversion@", "INSTALLDOCS_LINK_DOC=dkc-linux-base@source_suffix@@localversion@"),
                (" linux-base@source_suffix@@localversion@", " dkc-linux-base@source_suffix@@localversion@"),
                (" linux-headers-@abiname@@localversion@", " dkc-linux-headers-@abiname@@localversion@"),
                (" (module development meta-package)", " (module development metapackage)"),
            ],
        ),
        (
            "debian/templates/headers.meta.control.j2",
            [
                ("Package: linux-headers{{source_suffix}}{{localversion}}", "Package: dkc-linux-headers{{source_suffix}}{{localversion}}"),
                ("INSTALLDOCS_LINK_DOC=linux-base{{source_suffix}}{{localversion}}", "INSTALLDOCS_LINK_DOC=dkc-linux-base{{source_suffix}}{{localversion}}"),
                (" linux-base{{source_suffix}}{{localversion}}", " dkc-linux-base{{source_suffix}}{{localversion}}"),
                (" linux-headers-{{abiname}}{{localversion}}", " dkc-linux-headers-{{abiname}}{{localversion}}"),
                (" (module development meta-package)", " (module development metapackage)"),
            ],
        ),
    ),
    (
        "debian/templates/headers.tests-control.in",
        [OneOf(
            (
                "Depends: linux-headers-@abiname@@localversion@\n",
                "Depends: dkc-linux-headers-@abiname@@localversion@\n",
            ),
            # Older generations derive the test dependency from the renamed
            # generated package instead of naming it in the template.
            Absent("Depends:"),
        )],
    ),
    (
        "debian/templates/headers.meta.maintscript.in",
        [(" linux-headers-@abiname@@localversion@ ", " dkc-linux-headers-@abiname@@localversion@ ")],
    ),
    (
        "debian/templates/tools-versioned.control.in",
        [
            ("Package: linux-kbuild-@abiname@", "Package: dkc-linux-kbuild-@abiname@"),
            ("Build-Profiles: <!pkg.linux.notools>", "Build-Profiles: <!pkg.dkc.nokbuild>"),
            (
                "Depends: ${shlibs:Depends}, ${misc:Depends}, ${python3:Depends}, pahole",
                "Depends: ${shlibs:Depends}, ${misc:Depends}, pahole",
            ),
        ],
    ),
    (
        "debian/templates/image-dbg.control.in",
        [
            ("Package: linux-image-", "Package: dkc-linux-image-"),
            ("INSTALLDOCS_LINK_DOC=linux-base-", "INSTALLDOCS_LINK_DOC=dkc-linux-base-"),
            (" linux-base-@abiname@@localversion@", " dkc-linux-base-@abiname@@localversion@"),
            ("modules in linux-image-", "modules in dkc-linux-image-"),
        ],
    ),
    (
        "debian/templates/image-dbg.meta.control.in",
        [
            ("Package: linux-image@source_suffix@@localversion@-dbg", "Package: dkc-linux-image@source_suffix@@localversion@-dbg"),
            ("INSTALLDOCS_LINK_DOC=linux-base@source_suffix@@localversion@", "INSTALLDOCS_LINK_DOC=dkc-linux-base@source_suffix@@localversion@"),
            (" linux-base@source_suffix@@localversion@", " dkc-linux-base@source_suffix@@localversion@"),
            (" linux-image-@abiname@@localversion@-dbg", " dkc-linux-image-@abiname@@localversion@-dbg"),
            (" (debug symbols meta-package)", " (debug symbols metapackage)"),
        ],
    ),
    (
        "debian/templates/image-dbg.meta.maintscript.in",
        [(" linux-image-@abiname@@localversion@-dbg ", " dkc-linux-image-@abiname@@localversion@-dbg ")],
    ),
    (
        "debian/templates/image-extra-dev.control.in",
        [("Package: linux-bpf-dev", "Package: dkc-linux-bpf-dev")],
    ),
]

BINARY_POSTRM = """#!/bin/sh -e

version=@abiname@@localversion@
image_path=/boot/@image-stem@-$version

if [ "$1" = remove ]; then
    if command -v linux-run-hooks >/dev/null; then
        linux-run-hooks image postrm $version $image_path -- "$@"
    else
        echo >&2 'W: linux-base is not installed; cannot run postrm hooks'
    fi
fi

exit 0
"""

# Package names are namespaced, but the installed header paths are part of the
# Linux external-module interface.  Keep Debian's conventional, KREL-unique
# /usr/src/linux-headers-* layout instead of deriving payload paths from the
# renamed binary package.  Otherwise headers.links would point `source` at a
# path that the common package never creates, and tools which locate headers by
# the conventional path would fail even though dpkg considers the packages
# installed.
DKC_HEADER_PATHS = [
    (
        "debian/rules.real",
        [
            (
                "binary_kbuild: PREFIX_DIR = /usr/lib/$(PACKAGE_NAME)\n",
                "binary_kbuild: PREFIX_DIR = /usr/lib/linux-kbuild-$(ABINAME)\n",
            ),
            (
                "\tdh_link $(PREFIX_DIR) /usr/src/$(PACKAGE_NAME)\n",
                "\tdh_link $(PREFIX_DIR) /usr/src/linux-kbuild-$(ABINAME)\n",
            ),
            (
                "binary_headers-common: PACKAGE_NAME_KBUILD = linux-kbuild-$(ABINAME)\n"
                "binary_headers-common: BASE_DIR = /usr/src/$(PACKAGE_NAME)\n",
                "binary_headers-common: PACKAGE_NAME_KBUILD = linux-kbuild-$(ABINAME)\n"
                "binary_headers-common: BASE_DIR = /usr/src/linux-headers-$(ABINAME)-common$(LOCALVERSION)\n",
            ),
            (
                "binary_headers: PACKAGE_NAME_KBUILD = linux-kbuild-$(ABINAME)\n"
                "binary_headers: BASE_DIR = /usr/src/$(PACKAGE_NAME)\n",
                "binary_headers: PACKAGE_NAME_KBUILD = linux-kbuild-$(ABINAME)\n"
                "binary_headers: BASE_DIR = /usr/src/linux-headers-$(ABINAME)$(LOCALVERSION)\n",
            ),
        ],
    ),
    (
        "debian/templates/headers.install.j2",
        [
            (
                ".config .kernel* Module.symvers include  usr/src/{{package}}\n",
                ".config .kernel* Module.symvers include  usr/src/linux-headers-{{abiname}}{{localversion}}\n",
            ),
            (
                "scripts/module.lds                       usr/src/{{package}}/arch/{{kernel_arch}}\n",
                "scripts/module.lds                       usr/src/linux-headers-{{abiname}}{{localversion}}/arch/{{kernel_arch}}\n",
            ),
            (
                "arch/{{kernel_arch}}/include             usr/src/{{package}}/arch/{{kernel_arch}}\n",
                "arch/{{kernel_arch}}/include             usr/src/linux-headers-{{abiname}}{{localversion}}/arch/{{kernel_arch}}\n",
            ),
            (
                "arch/{{kernel_arch}}/lib/crtsavres.o     usr/src/{{package}}/arch/{{kernel_arch}}/lib\n",
                "arch/{{kernel_arch}}/lib/crtsavres.o     usr/src/linux-headers-{{abiname}}{{localversion}}/arch/{{kernel_arch}}/lib\n",
            ),
        ],
    ),
    (
        "debian/templates/headers.links.j2",
        [
            (
                "usr/lib/linux-kbuild-{{abiname}}/scripts                          usr/src/{{package}}/scripts\n",
                "usr/lib/linux-kbuild-{{abiname}}/scripts                          usr/src/linux-headers-{{abiname}}{{localversion}}/scripts\n",
            ),
            (
                "usr/lib/linux-kbuild-{{abiname}}/tools                            usr/src/{{package}}/tools\n",
                "usr/lib/linux-kbuild-{{abiname}}/tools                            usr/src/linux-headers-{{abiname}}{{localversion}}/tools\n",
            ),
            (
                "usr/src/{{package}}                                               usr/lib/modules/{{abiname}}{{localversion}}/build\n",
                "usr/src/linux-headers-{{abiname}}{{localversion}}                  usr/lib/modules/{{abiname}}{{localversion}}/build\n",
            ),
        ],
    ),
    (
        "debian/templates/headers.featureset.links.j2",
        [
            (
                "usr/lib/linux-kbuild-{{abiname}}/scripts  usr/src/{{package}}/scripts\n",
                "usr/lib/linux-kbuild-{{abiname}}/scripts  usr/src/linux-headers-{{abiname}}-common{{localversion}}/scripts\n",
            ),
            (
                "usr/lib/linux-kbuild-{{abiname}}/tools    usr/src/{{package}}/tools\n",
                "usr/lib/linux-kbuild-{{abiname}}/tools    usr/src/linux-headers-{{abiname}}-common{{localversion}}/tools\n",
            ),
        ],
    ),
]

GLOBAL_CONFIG = (
    "debian/config/config",
    [
        (
            "CONFIG_MODULE_SIG=y\n",
            "# DKC deliberately disables random per-build module signing. Archive,\n"
            "# module and UEFI signing are separate trust domains.\n"
            "# CONFIG_MODULE_SIG is not set\n",
        ),
        (
            "CONFIG_SECURITY_LOCKDOWN_LSM=y\n"
            "## choice: Kernel default lockdown mode\n"
            "CONFIG_LOCK_DOWN_KERNEL_FORCE_NONE=y\n"
            "## end choice\n"
            "CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT=y\n",
            "# SECURITY_LOCKDOWN_LSM selects MODULE_SIG whenever modules are enabled.\n"
            "# The initial unsigned product cannot truthfully retain EFI-triggered\n"
            "# lockdown without also designing the module/UEFI trust chain.\n"
            "# CONFIG_SECURITY_LOCKDOWN_LSM is not set\n"
            "# CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT is not set\n",
        ),
    ],
)

RULES_SECURITY = (
    "debian/rules.real",
    [
        (
            "\t\t-o MODULE_SIG_KEY=\\\"output/signing_key.pem\\\" \\\n",
            # Debian's config merge is followed by olddefconfig, and other
            # policy symbols can make signature sub-options visible again.
            # Use the generator's highest-precedence override for the parent
            # symbol instead of relying only on fragment ordering.
            "\t\t-o MODULE_SIG=n \\\n",
        ),
        (
            "$(STAMPS_DIR)/build_$(ARCH)_$(FEATURESET)_$(FLAVOUR): export "
            "KBUILD_SIGN_PIN = $(shell dd if=/dev/random bs=16 count=1 status=none | base64)\n",
            "",
        ),
        (
            """# Make sure the support for the used key type is built-in, CRYPTO_ECDSA for ecdsa keys.
\topenssl req -new -utf8 -sha256 -days 36500 \\
\t\t-batch -x509 -config certs/default_x509.genkey \\
\t\t-passout env:KBUILD_SIGN_PIN \\
\t\t-outform PEM -out $(DIR)/output/signing_key.pem \\
\t\t-keyout $(DIR)/output/signing_key.pem \\
\t\t-newkey ec -pkeyopt ec_paramgen_curve:secp384r1 2>&1

""",
            "",
        ),
        (
            "\t$(CROSS_COMPILE)strip --strip-debug "
            "$(OUTPUT_DIR)/boot/$(IMAGE_INSTALL_STEM)-$(REAL_VERSION)\n",
            "\t$(if $(LLVM_MAJOR),llvm-strip-$(LLVM_MAJOR),$(CROSS_COMPILE)strip) "
            "--strip-debug $(OUTPUT_DIR)/boot/$(IMAGE_INSTALL_STEM)-$(REAL_VERSION)\n",
        ),
        (
            "\trm $(DIR)/output/signing_key.pem\n\n",
            "",
        ),
        (
            "\t$(CROSS_COMPILE)objcopy -j .BTF -j .BTF_ids "
            "$(SOURCE_DIR)/vmlinux $(DIR)/vmlinux\n"
            "\tchmod 644 $(DIR)/vmlinux\n",
            "\tif grep -qx 'CONFIG_DEBUG_INFO_BTF=y' $(SOURCE_DIR)/.config; then \\\n"
            "\t\t$(if $(LLVM_MAJOR),llvm-objcopy-$(LLVM_MAJOR),$(CROSS_COMPILE)objcopy) "
            "-j .BTF -j .BTF_ids $(SOURCE_DIR)/vmlinux $(DIR)/vmlinux; \\\n"
            "\t\tchmod 644 $(DIR)/vmlinux; \\\n"
            "\telse \\\n"
            "\t\trm -f $(DIR)/vmlinux; \\\n"
            "\tfi\n",
        ),
        (
            "\techo \"ffffffffffffffff B The real System.map is in the "
            "linux-image-$(REAL_VERSION)-dbg package\" \\\n"
            "\t\t> $(OUTPUT_DIR)/boot/System.map-$(REAL_VERSION)\n",
            "ifneq (,$(filter pkg.linux.nokerneldbg,$(DEB_BUILD_PROFILES)))\n"
            "\tinstall -D -m644 $(DIR)/System.map "
            "$(OUTPUT_DIR)/boot/System.map-$(REAL_VERSION)\n"
            "else\n"
            "\techo \"ffffffffffffffff B The real System.map is in the "
            "linux-image-$(REAL_VERSION)-dbg package\" \\\n"
            "\t\t> $(OUTPUT_DIR)/boot/System.map-$(REAL_VERSION)\n"
            "endif\n",
        ),
        (
            "\tinstall -D -m644 $(DIR)/vmlinux $(OUTPUT_DIR_DBG_LIB)/vmlinux\n"
            "\tinstall -D -m644 $(DIR)/System.map $(OUTPUT_DIR_DBG_LIB)/System.map\n",
            "ifeq (,$(filter pkg.linux.nokerneldbg,$(DEB_BUILD_PROFILES)))\n"
            "\tinstall -D -m644 $(DIR)/vmlinux $(OUTPUT_DIR_DBG_LIB)/vmlinux\n"
            "\tinstall -D -m644 $(DIR)/System.map $(OUTPUT_DIR_DBG_LIB)/System.map\n"
            "endif\n",
        ),
        (
            "# cmd_depmod=: Don't run depmod to generate dependency files\n"
            "# cmd_sign=: Don't sign modules\n"
            "# suffix-y=: Don't compress modules\n"
            "\t+$(MAKE_CLEAN) -C $(DIR) modules_install \\\n"
            "\t\tcmd_depmod= \\\n"
            "\t\tcmd_sign= \\\n"
            "\t\tsuffix-y= \\\n"
            "\t\tINSTALL_MOD_PATH='$(CURDIR)/$(OUTPUT_DIR_DBG)'\n",
            "ifeq (,$(filter pkg.linux.nokerneldbg,$(DEB_BUILD_PROFILES)))\n"
            "# cmd_depmod=: Don't run depmod to generate dependency files\n"
            "# cmd_sign=: Don't sign modules\n"
            "# suffix-y=: Don't compress modules\n"
            "\t+$(MAKE_CLEAN) -C $(DIR) modules_install \\\n"
            "\t\tcmd_depmod= \\\n"
            "\t\tcmd_sign= \\\n"
            "\t\tsuffix-y= \\\n"
            "\t\tINSTALL_MOD_PATH='$(CURDIR)/$(OUTPUT_DIR_DBG)'\n"
            "endif\n",
        ),
        (
            "\trm -f $(OUTPUT_DIR_DBG)/lib/modules/$(REAL_VERSION)/build\n"
            "\trm -f $(OUTPUT_DIR_DBG)/lib/modules/$(REAL_VERSION)/source\n",
            "ifeq (,$(filter pkg.linux.nokerneldbg,$(DEB_BUILD_PROFILES)))\n"
            "\trm -f $(OUTPUT_DIR_DBG)/lib/modules/$(REAL_VERSION)/build\n"
            "\trm -f $(OUTPUT_DIR_DBG)/lib/modules/$(REAL_VERSION)/source\n"
            "endif\n",
        ),
        (
            "\tinstall -d $(CURDIR)/$(OUTPUT_DIR_DBG)/lib/modules/$(REAL_VERSION)/vdso\n"
            "\t+$(MAKE_CLEAN) -C $(DIR) vdso_install \\\n"
            "\t\tcmd_symlink= \\\n"
            "\t\tINSTALL_MOD_PATH='$(CURDIR)/$(OUTPUT_DIR_DBG)'\n",
            "ifeq (,$(filter pkg.linux.nokerneldbg,$(DEB_BUILD_PROFILES)))\n"
            "\tinstall -d $(CURDIR)/$(OUTPUT_DIR_DBG)/lib/modules/$(REAL_VERSION)/vdso\n"
            "\t+$(MAKE_CLEAN) -C $(DIR) vdso_install \\\n"
            "\t\tcmd_symlink= \\\n"
            "\t\tINSTALL_MOD_PATH='$(CURDIR)/$(OUTPUT_DIR_DBG)'\n"
            "endif\n",
        ),
    ],
)

# Every tool the kernel would otherwise bind to a GNU name.
# The major comes from the $(LLVM_MAJOR) makeflag rather than being baked in,
# so defines.toml stays the single source of truth and bumping the compiler
# does not require regenerating this patch.
LLVM_TOOLS = [
    ("LLVM", "-$(LLVM_MAJOR)"),
    ("LLVM_PREFIX", ""),
    ("LLVM_SUFFIX", "-$(LLVM_MAJOR)"),
    ("CC", "$$(if $$(DEBIAN_KERNEL_USE_CCACHE),$$(CCACHE)) clang-$(LLVM_MAJOR)"),
    ("HOSTCC", "clang-$(LLVM_MAJOR)"),
    ("HOSTCXX", "clang++-$(LLVM_MAJOR)"),
    ("LD", "ld.lld-$(LLVM_MAJOR)"),
    ("AR", "llvm-ar-$(LLVM_MAJOR)"),
    ("NM", "llvm-nm-$(LLVM_MAJOR)"),
    ("OBJCOPY", "llvm-objcopy-$(LLVM_MAJOR)"),
    ("OBJDUMP", "llvm-objdump-$(LLVM_MAJOR)"),
    ("READELF", "llvm-readelf-$(LLVM_MAJOR)"),
    ("STRIP", "llvm-strip-$(LLVM_MAJOR)"),
    ("LLVM_LINK", "llvm-link-$(LLVM_MAJOR)"),
]

PATCHES = {
    "0000-debian-13-kernel-image-layout.patch": [
        LAYOUT_GENCONTROL,
        LAYOUT_RULES,
        *LAYOUT_TEMPLATES,
    ],
    "0001-select-llvm-toolchain.patch": [CONFIG_SCHEMA, DEFINES, GENCONTROL],
    "0002-drive-kbuild-with-llvm.patch": [RULES_REAL],
    "0003-disable-random-module-signing.patch": [
        AMD64_DEFINES,
        GLOBAL_CONFIG,
        RULES_SECURITY,
    ],
    "0004-x86-64-flavours.patch": [
        KCONFIG_CPU,
        X86_MAKEFILE,
        AMD64_FLAVOURS,
    ],
    "0005-dkc-package-namespace.patch": [
        DKC_GENCONTROL,
        DKC_DEBIAN_RELEASE,
        *PACKAGE_TEMPLATES,
        *DKC_HEADER_PATHS,
    ],
}

NEW_FILES = {
    "0004-x86-64-flavours.patch": FLAVOUR_CONFIGS,
    "0005-dkc-package-namespace.patch": {
        "debian/templates/binary.postrm.in": BINARY_POSTRM,
    },
}


def kernelvariables_block(llvm_major: int) -> str:
    lines = []
    for name, value in LLVM_TOOLS:
        rendered = value.replace("@LLVM_MAJOR@", str(llvm_major))
        lines.append(
            f"\techo '{name} = {rendered}' >> '$(DIR)/.kernelvariables'\n"
        )
    return "".join(lines)


def _variant_matches(text: str, variant: tuple[str, str] | Absent) -> bool:
    if isinstance(variant, Absent):
        return variant.marker not in text
    return text.count(variant[0]) == 1


def _render(replacement: str, llvm_major: int) -> str:
    # Explicit markers rather than str.format: the replacements contain
    # literal braces from the Python and Make code they insert, which
    # format() would try to interpret as fields.
    return replacement.replace(
        "@KERNELVARIABLES@", kernelvariables_block(llvm_major)
    ).replace("@LLVM_MAJOR@", str(llvm_major))


def _apply_line_pattern(path: str, text: str, edit: LinePattern, llvm_major: int) -> str:
    matches = list(re.finditer(rf"^(?:{edit.regex})\n", text, re.MULTILINE))
    if len(matches) != 1:
        problem = "no longer matches exactly one line" if not matches else "is ambiguous"
        raise SystemExit(
            f"line pattern {problem} in {path}; the Debian source changed and "
            f"the overlay must be reviewed:\n---\n{edit.regex}\n---"
        )
    match = matches[0]
    rendered = _render(edit.replacement, llvm_major).replace("@LINE@", match[0])
    return text[: match.start()] + rendered + text[match.end() :]


def apply_edit(path: str, text: str, edit: object, llvm_major: int) -> str:
    """Apply one exact edit, choosing the single matching reviewed spelling."""
    if isinstance(edit, LinePattern):
        return _apply_line_pattern(path, text, edit, llvm_major)
    variants = edit.variants if isinstance(edit, OneOf) else (edit,)
    matches = [variant for variant in variants if _variant_matches(text, variant)]
    if len(matches) != 1:
        first = variants[0]
        anchor = first.marker if isinstance(first, Absent) else first[0]
        problem = "no longer matches exactly once" if not matches else "is ambiguous"
        raise SystemExit(
            f"anchor {problem} in {path}; the Debian source changed and the "
            f"overlay must be reviewed:\n---\n{anchor[:200]}\n---"
        )
    variant = matches[0]
    if isinstance(variant, Absent):
        return text
    anchor, replacement = variant
    return text.replace(anchor, _render(replacement, llvm_major), 1)


def unified_diff(path: str, before: str | None, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            [] if before is None else before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="/dev/null" if before is None else f"a/{path}",
            tofile=f"b/{path}",
        )
    )


@dataclass(frozen=True)
class EditedTree:
    """The ordered series applied in memory to one source tree."""

    patches: dict[str, str]
    original: dict[str, str | None]
    files: dict[str, str]


def edit_tree(root: pathlib.Path, llvm_major: int) -> EditedTree:
    """Apply every edit of the ordered series in memory."""
    tree: dict[str, str] = {}
    original: dict[str, str | None] = {}

    def current(path: str) -> str:
        if path not in tree:
            # No newline translation: an applied file keeps every byte that
            # no edit touches.
            tree[path] = (root / path).read_bytes().decode("utf-8")
            original[path] = tree[path]
        return tree[path]

    patches: dict[str, str] = {}
    for name, groups in PATCHES.items():
        chunks = []
        for group in groups:
            if isinstance(group, FileVariants):
                present = [item for item in group.groups if (root / item[0]).is_file()]
                if len(present) != 1:
                    names = ", ".join(item[0] for item in group.groups)
                    raise SystemExit(
                        f"exactly one reviewed file name must exist: {names}"
                    )
                group = present[0]
            path, edits = group
            before = current(path)
            after = before
            for edit in edits:
                after = apply_edit(path, after, edit, llvm_major)
            tree[path] = after
            chunks.append(unified_diff(path, before, after))
        for path, content in sorted(NEW_FILES.get(name, {}).items()):
            if (root / path).exists() or path in tree:
                raise SystemExit(
                    f"new overlay file {path} now exists upstream; review the collision"
                )
            tree[path] = content
            original[path] = None
            chunks.append(unified_diff(path, None, content))
        if patch := "".join(chunks):
            patches[name] = patch
    return EditedTree(patches, original, tree)


def generate(root: pathlib.Path, llvm_major: int) -> dict[str, str]:
    """Return every non-empty patch of the ordered series for one source tree."""
    return edit_tree(root, llvm_major).patches


_HUNK_HEADER = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")

ChangeGroup = tuple[str, tuple[str, ...], tuple[str, ...]]


def change_groups(patch: str) -> list[ChangeGroup]:
    """Reduce a unified diff to its removed and added lines, in order.

    Hunk positions and context lines are dropped, and every hunk is re-diffed on
    its own, so two diffs of the same edits compare equal even when the text
    around an edit, and with it difflib's alignment, differs between sources.
    """
    lines = patch.splitlines(keepends=True)
    groups: list[ChangeGroup] = []
    index = 0
    while index < len(lines):
        if not (
            lines[index].startswith("--- ")
            and index + 1 < len(lines)
            and lines[index + 1].startswith("+++ ")
        ):
            raise ValueError(f"diff line {index + 1} is not a file header")
        header = f"{lines[index][4:].rstrip()} {lines[index + 1][4:].rstrip()}"
        index += 2
        while index < len(lines) and (match := _HUNK_HEADER.match(lines[index])):
            old_count = 1 if match[1] is None else int(match[1])
            new_count = 1 if match[2] is None else int(match[2])
            index += 1
            old: list[str] = []
            new: list[str] = []
            while len(old) < old_count or len(new) < new_count:
                if index >= len(lines):
                    raise ValueError(f"truncated hunk in {header}")
                tag, text = lines[index][:1], lines[index][1:]
                if tag not in (" ", "-", "+"):
                    raise ValueError(f"diff line {index + 1} is not a hunk line")
                if tag != "+":
                    old.append(text)
                if tag != "-":
                    new.append(text)
                index += 1
            if (len(old), len(new)) != (old_count, new_count):
                raise ValueError(f"hunk line counts do not match in {header}")
            matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
            groups.extend(
                (header, tuple(old[i1:i2]), tuple(new[j1:j2]))
                for tag, i1, i2, j1, j2 in matcher.get_opcodes()
                if tag != "equal"
            )
    return groups


def _render_groups(groups: list[ChangeGroup]) -> list[str]:
    lines = []
    for header, old, new in groups:
        lines.append(f"@ {header}\n")
        lines.extend(f"-{line}" for line in old)
        lines.extend(f"+{line}" for line in new)
    return [line if line.endswith("\n") else line + "\n" for line in lines]


def apply(root: pathlib.Path, llvm_major: int, reviewed: pathlib.Path) -> None:
    """Edit `root` in place, but only as the committed patches describe."""
    edited = edit_tree(root, llvm_major)
    committed = {
        path.name: path.read_bytes().decode("utf-8")
        for path in sorted(reviewed.glob("*.patch"))
    }
    if set(committed) != set(edited.patches):
        raise SystemExit(
            "the committed overlay is not the series this source needs: "
            f"missing={sorted(set(edited.patches) - set(committed))}, "
            f"unexpected={sorted(set(committed) - set(edited.patches))}; "
            "regenerate it with `make overlay-patches` and review the result"
        )
    notes = []
    for name, patch in edited.patches.items():
        try:
            expected = change_groups(committed[name])
        except ValueError as error:
            raise SystemExit(
                f"committed {name} is not a readable unified diff: {error}"
            ) from error
        actual = change_groups(patch)
        if expected != actual:
            difference = list(
                difflib.unified_diff(
                    _render_groups(expected),
                    _render_groups(actual),
                    f"committed/{name}",
                    f"generated/{name}",
                    n=1,
                )
            )
            raise SystemExit(
                f"{name} does not describe the lines the overlay changes in this "
                f"source with LLVM {llvm_major}; regenerate it with "
                "`make overlay-patches` and review the difference:\n"
                + "".join(difference[:80])
                + ("[difference truncated]\n" if len(difference) > 80 else "")
            )
        drifted = patch != committed[name]
        notes.append(
            f"  {name}"
            + (" (context differs from the reviewed source; changed lines match)" if drifted else "")
        )
    for path, content in edited.files.items():
        if content == edited.original[path]:
            continue
        target = root / path
        if target.is_symlink():
            raise SystemExit(f"refusing to write the overlay through symlink {path}")
        if edited.original[path] is None:
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    print("\n".join(notes), file=sys.stderr)


USAGE = (
    "usage: generate-overlay-patches.py <source-root> <llvm-major> <output-dir>\n"
    "       generate-overlay-patches.py --apply <source-root> <llvm-major> <patch-dir>"
)


def main() -> int:
    arguments = sys.argv[1:]
    applying = arguments[:1] == ["--apply"]
    if applying:
        arguments = arguments[1:]
    if len(arguments) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    root = pathlib.Path(arguments[0])
    llvm_major = int(arguments[1])
    if applying:
        reviewed = pathlib.Path(arguments[2])
        if not reviewed.is_dir():
            print("committed patch directory does not exist", file=sys.stderr)
            return 2
        apply(root, llvm_major, reviewed)
        return 0
    output = pathlib.Path(arguments[2])
    if not output.is_dir() or any(output.iterdir()):
        print("output directory must exist and be empty", file=sys.stderr)
        return 2

    for name, patch in generate(root, llvm_major).items():
        (output / name).write_text(patch)
        print(f"generated {name} ({len(patch.splitlines())} lines)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
