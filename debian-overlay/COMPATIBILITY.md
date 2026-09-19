# Debian 13 build-feasibility report

What the current Debian Sid kernel source demands, what Debian 13 provides, and
what DKC changes to close the difference. The rows below were measured against
`linux` 7.1.7-1 on 2026-08-11 and reviewed again for `linux` 7.2.6-1, in a clean
Trixie container, with the build profiles of the matching source profile in
`config/source-profiles/`. The compiler row names whichever GCC the source
selects: Debian Sid moved from `gcc-15` to `gcc-16` in 7.2.

Regenerate the evidence with:

```sh
make release-preflight  # source, overlay, toolchain, package graph, dependencies
make closure-proof      # every installed package resolved to its origin
```

## The gap

| Upstream dependency | Trixie availability | Adaptation | Files changed |
|---|---|---|---|
| `gcc-15-for-host`, `gcc-16-for-host` | **absent.** Trixie has `gcc-14-for-host` 14.2.0-19 and no newer GCC at all | Select the LLVM toolchain instead of a newer GCC, and depend on the real `clang-21` package | `debian/config/defines.toml`, `debian/lib/python/debian_linux/config_v2.py`, `debian/bin/gencontrol.py` |
| `gcc-<major>-<gnutype>` (cross) | absent | Not synthesised when LLVM is selected: Clang is never invoked through a GNU triplet prefix | `debian/bin/gencontrol.py` |
| kernel image below `/usr/lib/modules`, `Pre-Depends: linux-base (>= 4.17~)` (Linux 7.2) | **absent.** Trixie has `linux-base` 4.12.1, whose bootloader and initramfs hooks read `/boot` | Keep the image, configuration and `System.map` in `/boot` and require only `linux-base (>= 4.12~)` | `debian/rules.real`, `debian/bin/gencontrol.py`, `debian/templates/base.install.j2`, `debian/templates/binary.install.j2`, `debian/templates/image.control.in` |
| other source-declared dependencies | present | none | none |

For 7.1.7-1 the overlay generated 24 distinct amd64 build dependencies under
the project profile set, all present in the build image, with
`dpkg-checkbuilddeps` reporting **all satisfied** against 28 generated control
references to the unavailable `gcc-15-for-host` before the overlay.
`make release-preflight` re-measures both numbers for the source under audit and
fails on any unsatisfied dependency.

## What the overlay does

**`0000-debian-13-kernel-image-layout.patch`** — where the kernel is installed.

Linux 7.2 packaging installs `vmlinuz`, the configuration and `System.map`
below the modules directory and relies on `linux-base` 4.17 hooks to copy them
into `/boot`. Debian 13 has `linux-base` 4.12, whose hooks read `/boot`, so the
patch restores the established paths and the `linux-base (>= 4.12~)` floor that
Debian 13 can satisfy. Series that already install into `/boot` need no such
patch and do not carry one.

**`0001-select-llvm-toolchain.patch`** — the compiler that is *declared*.

- adds an optional `llvm_major` to the configuration schema and sets it in
  `defines.toml`, so the choice lives in the packaging source of truth and not
  in a generated file;
- makes the generator depend on `clang-<major>`, `lld-<major>` and
  `llvm-<major>` as **three separate dependencies**. An alternatives group
  would let apt satisfy the relation with Clang alone and leave the build with
  no linker;
- never fabricates `clang-<major>-for-host`. LLVM ships no such package, so the
  dependency could never be satisfied;
- suppresses the GNU-triplet cross-compiler dependency when LLVM is selected.

**`0002-drive-kbuild-with-llvm.patch`** — the compiler that is *used*.

This is a separate patch because the first one alone is a trap: dependencies
would resolve while the build still invoked the GCC that Debian selected.

- passes `LLVM=-<major>` on the Kbuild command line through `MAKE_CLEAN`, the
  single wrapper Debian uses for every Kbuild entry point;
- writes every tool name into `.kernelvariables`: `CC`, `HOSTCC`, `HOSTCXX`,
  `LD`, `AR`, `NM`, `OBJCOPY`, `OBJDUMP`, `READELF`, `STRIP`, `LLVM_LINK`.

The second point is not redundancy. The kernel binds those variables from
`$(LLVM)` at lines 428–521 of its Makefile, while Debian's own patch includes
`.kernelvariables` at roughly line 561. Setting `LLVM=` there would arrive after
the tools were already bound to their GNU names. Naming each tool is also what
an out-of-tree build needs: a plain
`make -C /usr/lib/modules/<release>/build M=$PWD modules` passes no `LLVM=` and
would otherwise compile a module with GCC against a kernel built with Clang.

## Security and reproducibility impact

| Aspect | Effect |
|---|---|
| Rust | Unchanged and still enabled. `pkg.linux.norust` is deliberately not in the profile set |
| BTF and unrelated hardening | Unchanged by the overlay |
| Module signing and lockdown | Explicitly disabled for the initial Secure-Boot-off product. `SECURITY_LOCKDOWN_LSM` selects `MODULE_SIG`, so retaining EFI-triggered lockdown while disabling module signatures is not a valid kernel configuration. Both return only with a protected module/UEFI key lifecycle and Secure-Boot-on tests |
| `CC_HAS_COUNTED_BY` | **Preserved** by choosing Clang 21. Clang 19 would have disabled it, which Debian's own GCC build has enabled; that is why `clang-19` from `trixie` main was rejected despite needing no extra suite |
| `LD_CAN_USE_KEEP_IN_OVERLAY` | Preserved: needs lld >= 21 |
| `CC_HAS_COUNTED_BY_PTR` | Not available: needs Clang >= 22.1. Debian's own GCC build does not have it either, so this is not a regression against the stock kernel |
| Client trust path | No third-party repository. The headers package depends on `clang-21` from `trixie-backports`, which is official Debian signed by the archive key already present on every Debian 13 system |
| Reproducibility | Improved relative to a third-party toolchain: Debian's archive and `snapshot.debian.org` retain the exact `.deb` inputs |

## Real-build validation

1. **bindgen and libclang.** Debian's `bindgen` depends on the *unversioned*
   `libclang-dev`, which on Trixie is libclang 19, so both 19 and 21 are
   installed. bindgen resolves libclang with `dlopen` at runtime and would
   happily load the older one, generating Rust bindings from a different
   compiler than the kernel is built with. `LIBCLANG_PATH` is pinned to
   `/usr/lib/llvm-21/lib` and the image asserts that `libclang.so` there
   resolves to major 21. The image exercises bindgen's actual dynamic-loading
   path under `LD_DEBUG=libs`, requires the selected library, and rejects a
   loaded libclang 19. The real configuration separately requires
   `CONFIG_RUST=y` after `olddefconfig`.
2. **Debian's own binutils calls.** `debian/rules.real` runs
   `$(CROSS_COMPILE)strip` and `$(CROSS_COMPILE)objcopy` directly, outside
   Kbuild, to strip the image and to extract BTF sections. The overlay selects
   the exact versioned LLVM tools for these operations. The real package build
   preserves BTF in `vmlinux`, the headers copy, and sampled modules.
3. **`DEBIAN_KERNEL_NO_CC_VERSION_CHECK`.** Debian sets this in
   `.kernelvariables`, which suppresses the kernel's compiler version check.
   The build therefore verifies the source's own LLVM minimum before
   configuration, requires the final Kconfig compiler probes to identify the
   selected major, and reconciles the saved commands and ELF producer strings.
4. **`CROSS_COMPILE` remains set** to `x86_64-linux-gnu-` for a native build.
   With `LLVM=` the kernel only uses it to derive `--target=`, which is correct
   here. The generated `.cmd` inventory rejects GNU, unversioned LLVM, and any
   LLVM tool from a different major.

## Revalidation trigger

The overlay is generated from anchored text by
`scripts/in-container/generate-overlay-patches.py`. When Debian publishes a new
`src:linux` and any anchor no longer matches, generation fails and names the
anchor rather than applying with fuzz into a subtly different tree. An edit that
Debian spells differently between packaging generations keeps every reviewed
spelling, so one generator still regenerates an older series' overlay.

Re-run all three audits for every new `DEBIAN_SOURCE_VERSION`, and re-check this
report whenever `defines.toml` changes `c_compiler`, `enable_rust`, or
`enable_signed`, when the kernel image layout or its `linux-base` floor changes,
or when the profile's `build_profiles` set changes.
