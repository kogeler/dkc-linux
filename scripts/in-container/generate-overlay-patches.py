#!/usr/bin/env python3
"""Generate the DKC packaging overlay against a given Debian kernel source.

The overlay is defined here as exact anchored edits, and the `.patch` files
under `debian-overlay/patches/` are its output. Both are committed: the patches
are what a reviewer reads, this file is what regenerates them when Debian
publishes a new source version.

That matters because the anchors are the revalidation trigger. If Debian changes
one of these lines, generation fails loudly with the anchor that no longer
matches, instead of a patch applying with fuzz into something subtly different.

Runs inside the build container. Writes the patches to stdout as a single
stream, one `--- a/… +++ b/…` section per file.
"""

from __future__ import annotations

import difflib
import pathlib
import sys

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

DEFINES = (
    "debian/config/defines.toml",
    [(
        "c_compiler = 'gcc-15'\n",
        "c_compiler = 'gcc-15'\nllvm_major = @LLVM_MAJOR@\n",
    )],
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
            "MAKE_CLEAN = $(setup_env) $(MAKE) \\\n"
            "\tKCFLAGS=-fdebug-prefix-map=$(CURDIR)/= \\\n",
            "# LLVM=-<major> belongs on the command line, not in .kernelvariables:\n"
            "# the kernel binds CC, LD, AR and the rest from $(LLVM) near the top of\n"
            "# its Makefile, while Debian includes .kernelvariables about 130 lines\n"
            "# further down. A command-line variable is visible from the start of\n"
            "# parsing, and MAKE_CLEAN wraps every Kbuild entry point, so setting it\n"
            "# once here covers configuration, kernel, modules and headers alike.\n"
            "MAKE_CLEAN = $(setup_env) $(MAKE) \\\n"
            "\t$(if $(LLVM_MAJOR),LLVM=-$(LLVM_MAJOR)) \\\n"
            "\tKCFLAGS=-fdebug-prefix-map=$(CURDIR)/= \\\n",
        ),
        (
            "ifeq (./,$(dir $(C_COMPILER)))\n"
            "\techo 'CC = $$(if $$(DEBIAN_KERNEL_USE_CCACHE),$$(CCACHE)) "
            "$$(CROSS_COMPILE)$(C_COMPILER)' >> '$(DIR)/.kernelvariables'\n"
            "else\n",
            "ifdef LLVM_MAJOR\n"
            "# .kernelvariables is included too late for LLVM= to select the toolchain,\n"
            "# so every tool is set by name. That is also what an out-of-tree build\n"
            "# needs: a plain `make -C /usr/lib/modules/<rel>/build M=$$PWD modules`\n"
            "# passes no LLVM= and would otherwise use GNU tools against a kernel\n"
            "# built with Clang. No GNU triplet is prepended, because\n"
            "# x86_64-linux-gnu-clang-N does not exist.\n"
            "@KERNELVARIABLES@"
            "else ifeq (./,$(dir $(C_COMPILER)))\n"
            "\techo 'CC = $$(if $$(DEBIAN_KERNEL_USE_CCACHE),$$(CCACHE)) "
            "$$(CROSS_COMPILE)$(C_COMPILER)' >> '$(DIR)/.kernelvariables'\n"
            "else\n",
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

KCONFIG_CPU = (
    "arch/x86/Kconfig.cpu",
    [(
        "config X86_NATIVE_CPU\n"
        "\tbool \"Build and optimize for local/native CPU\"\n"
        "\tdepends on X86_64\n"
        "\tdepends on CC_HAS_MARCH_NATIVE\n"
        "\thelp\n"
        "\t  Optimize for the current CPU used to compile the kernel.\n"
        "\t  Use this option if you intend to build the kernel for your\n"
        "\t  local machine.\n"
        "\n"
        "\t  Note that such a kernel might not work optimally on a\n"
        "\t  different x86 machine.\n"
        "\n"
        "\t  If unsure, say N.\n",
        "config X86_NATIVE_CPU\n"
        "\tbool \"Build and optimize for local/native CPU\"\n"
        "\tdepends on X86_64\n"
        "\tdepends on CC_HAS_MARCH_NATIVE\n"
        "\thelp\n"
        "\t  Optimize for the current CPU used to compile the kernel.\n"
        "\t  Use this option if you intend to build the kernel for your\n"
        "\t  local machine.\n"
        "\n"
        "\t  Note that such a kernel might not work optimally on a\n"
        "\t  different x86 machine.\n"
        "\n"
        "\t  If unsure, say N.\n"
        "\n"
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
        "endchoice\n",
    )],
)

X86_MAKEFILE = (
    "arch/x86/Makefile",
    [
        (
            "KBUILD_CFLAGS += -mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx -mno-sse4a\n"
            "KBUILD_RUSTFLAGS += --target=$(objtree)/scripts/target.json\n"
            "KBUILD_RUSTFLAGS += -Ctarget-feature=-sse,-sse2,-sse3,-ssse3,-sse4.1,-sse4.2,-avx,-avx2\n",
            "X86_CFLAGS_NO_SIMD := -mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx -mno-sse4a\n"
            "X86_RUSTFLAGS_NO_SIMD := -Ctarget-feature=-sse,-sse2,-sse3,-ssse3,-sse4.1,-sse4.2,-avx,-avx2\n"
            "KBUILD_CFLAGS += $(X86_CFLAGS_NO_SIMD)\n"
            "KBUILD_RUSTFLAGS += --target=$(objtree)/scripts/target.json\n"
            "KBUILD_RUSTFLAGS += $(X86_RUSTFLAGS_NO_SIMD)\n",
        ),
        (
            "ifdef CONFIG_X86_NATIVE_CPU\n"
            "        KBUILD_CFLAGS += -march=native\n"
            "        KBUILD_RUSTFLAGS += -Ctarget-cpu=native\n"
            "else\n"
            "        KBUILD_CFLAGS += -march=x86-64 -mtune=generic\n"
            "        KBUILD_RUSTFLAGS += -Ctarget-cpu=x86-64 -Ztune-cpu=generic\n"
            "endif\n",
            "ifdef CONFIG_X86_NATIVE_CPU\n"
            "        KBUILD_CFLAGS += -march=native\n"
            "        KBUILD_RUSTFLAGS += -Ctarget-cpu=native\n"
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
    [(
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
    )],
)

DKC_DEBIAN_RELEASE = (
    "debian/config/defines.toml",
    [
        (
            "[[debianrelease]]\n"
            "name_regex = 'unstable'\n"
            "abi_suffix = '+deb14'\n"
            "revision_regex = '\\d+(\\.\\d+)?'\n",
            "[[debianrelease]]\n"
            "name_regex = 'trixie'\n"
            "abi_suffix = '+dkc13'\n"
            "revision_regex = '\\d+(\\.\\d+)?\\+dkc13\\.\\d+'\n"
            "\n"
            "[[debianrelease]]\n"
            "name_regex = 'unstable'\n"
            "abi_suffix = '+deb14'\n"
            "revision_regex = '\\d+(\\.\\d+)?'\n",
        ),
        (
            "[build]\n"
            "c_compiler = 'gcc-15'\n",
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
            "[build]\n"
            "c_compiler = 'gcc-15'\n",
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


def kernelvariables_block(llvm_major: int) -> str:
    lines = []
    for name, value in LLVM_TOOLS:
        rendered = value.replace("@LLVM_MAJOR@", str(llvm_major))
        lines.append(
            f"\techo '{name} = {rendered}' >> '$(DIR)/.kernelvariables'\n"
        )
    return "".join(lines)


def apply_edits(root: pathlib.Path, path: str, edits: list, llvm_major: int) -> str:
    """Apply anchored edits to one file and return its unified diff."""
    target = root / path
    original = target.read_text()
    updated = original
    for anchor, replacement in edits:
        if anchor not in updated:
            raise SystemExit(
                f"anchor no longer present in {path}; the Debian source changed "
                f"and the overlay must be reviewed:\n---\n{anchor[:200]}\n---"
            )
        # Explicit markers rather than str.format: the replacements contain
        # literal braces from the Python and Make code they insert, which
        # format() would try to interpret as fields.
        rendered = replacement.replace(
            "@KERNELVARIABLES@", kernelvariables_block(llvm_major)
        ).replace("@LLVM_MAJOR@", str(llvm_major))
        updated = updated.replace(anchor, rendered, 1)

    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def add_file(root: pathlib.Path, path: str, content: str) -> str:
    """Return a creation diff, refusing to hide an upstream file collision."""
    target = root / path
    if target.exists():
        raise SystemExit(
            f"new overlay file {path} now exists upstream; review the collision"
        )
    return "".join(
        difflib.unified_diff(
            [],
            content.splitlines(keepends=True),
            fromfile="/dev/null",
            tofile=f"b/{path}",
        )
    )


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: generate-overlay-patches.py <source-root> <llvm-major> <patch-name>",
              file=sys.stderr)
        return 2
    root = pathlib.Path(sys.argv[1])
    llvm_major = int(sys.argv[2])
    name = sys.argv[3]

    if name not in PATCHES:
        print(f"unknown patch {name!r}; known: {sorted(PATCHES)}", file=sys.stderr)
        return 2

    for path, edits in PATCHES[name]:
        sys.stdout.write(apply_edits(root, path, edits, llvm_major))
    if name == "0004-x86-64-flavours.patch":
        for path, content in sorted(FLAVOUR_CONFIGS.items()):
            sys.stdout.write(add_file(root, path, content))
    if name == "0005-dkc-package-namespace.patch":
        sys.stdout.write(
            add_file(root, "debian/templates/binary.postrm.in", BINARY_POSTRM)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
