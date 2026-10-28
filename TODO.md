# Pending work

## Switch the kernel toolchain to Clang/LLVM 22

### Why

DKC compiles and links the kernel with `clang-21`, `lld-21`, and `llvm-21`
from `trixie-backports`. The headers packages depend on those tools so that
DKMS and out-of-tree modules use the kernel's own compiler. That dependency is
the only reason users have to enable Debian backports; the kernel image itself
never needs it.

Debian 13 now ships LLVM 22 in its main `trixie` suite, at
`1:22.1.8-1~deb13u4` as of 2026-10-28, with a newer revision in
`trixie-proposed-updates`. That covers every LLVM package DKC installs:
`clang-22`, `lld-22`, `llvm-22`, `libclang1-22`, `libclang-22-dev`,
`libclang-common-22-dev`, and `libclang-rt-22-dev`. Switching to LLVM 22 would
therefore:

- let users install headers from plain Debian 13 without backports;
- remove the risk that the `trixie-backports` LLVM 21 packages, which published
  headers depend on, stop being maintained now that stable carries a newer
  LLVM;
- give Linux 7.2 `CC_HAS_COUNTED_BY_PTR` (pointer bounds annotations) and the
  fixed `__builtin_counted_by_ref` (`CC_HAS_BROKEN_COUNTED_BY_REF` unset);
- put rustc and Clang on the same LLVM major again.

### Current state

- `trixie-backports` moved `rustc` from 1.95 (LLVM 21) to 1.96.1, which links
  the stable `libllvm22`, and withdrew the LLVM 21 build for amd64. The kernel
  build image used to require equal rustc and Clang LLVM majors and stopped
  building. It now accepts the pair. Linux compiles Rust to native objects that
  kernel LTO does not merge, and the only option that needs equal majors, the
  experimental `RUST_INLINE_HELPERS`, is unset in Debian's configuration. See
  [docs/BUILD.md](docs/BUILD.md).
- No DKC kernel has been built with rustc 1.96 yet. The first release build on
  the refreshed image is the first complete qualification of that pair. Debian
  itself builds Linux 7.2.12 with rustc 1.97.1 and libclang 21, so the pair is
  expected to work for 7.2. For 7.1 there is no equivalent evidence: Debian
  built 7.1.8 with rustc 1.95.
- Headers users still need `trixie-backports`, and the published packages keep
  depending on `clang-21`, `lld-21`, and `llvm-21`.

### Blocker: bindgen with libclang 22

Debian 13's `bindgen` is 0.71.1. With libclang 22 it turns any C struct that is
declared before its definition, directly or through a typedef, into an opaque
`_address: u8` placeholder. That loses every field and the real size
(rust-bindgen issues 3264 and 3275, fixed by PR 3278 in bindgen 0.72.1).

- Linux declares many structs ahead of their definitions and generates its
  bindings with `--no-layout-tests`. Nothing in bindgen catches a wrong layout,
  so the result is either a failed Rust build or Rust code that uses wrong
  struct sizes.
- Linux 7.2 and 7.3 `scripts/rust_is_available.sh` only warn about
  "bindgen < 0.72.1 together with libclang >= 22", and Linux 7.1 does not
  check at all.
- bindgen 0.72.1 exists only in Debian Sid. It is not in `trixie`,
  `trixie-updates`, or `trixie-backports`.
- The build image's bindgen probe reproduces the defect and fails with
  `LLVM_MAJOR=22`.
- Using Clang 22 with bindgen on libclang 21 is not a way around this. Linux
  warns about the libclang/Clang mismatch, Clang-22-only compiler flags would
  reach libclang 21, and the build would still depend on LLVM 21 from
  backports.

### Compatibility review

This review covers the Debian sources `7.1.8-2` and `7.2.12-1`, and the upcoming
`7.3~rc7-1~exp1` as a forward check.

| | Linux 7.1 | Linux 7.2 | Linux 7.3-rc7 |
|---|---|---|---|
| Minimum LLVM (`scripts/min-tool-version.sh`) | 15.0.0 | 17.0.1 | 17.0.1 |
| Minimum rustc on x86 | 1.85.0 | 1.85.0 | 1.85.0 |
| Minimum bindgen | 0.71.1 | 0.71.1 | 0.71.1 |
| Kernel check for bindgen < 0.72.1 with libclang 22 | none | warning | warning |
| Clang 22 | supported | supported, with 22.1-specific Kconfig | supported |
| rustc 1.96 | no Debian build evidence | Debian built 7.2.12 with 1.97.1 | not reviewed |

`Documentation/process/changes.rst` supports the latest formal LLVM release and
asks only for "a recent version" of Rust, with no upper bound. On Linux 7.2,
Clang 22.1 changes these Kconfig results compared with Clang 21:

- `CC_HAS_COUNTED_BY_PTR=y`;
- `CC_HAS_BROKEN_COUNTED_BY_REF` unset;
- `CC_HAS_ALLOC_TOKEN=y`, with no effect while Debian's configuration leaves
  `KMALLOC_PARTITION_CACHES` disabled;
- `RUSTC_CLANG_LLVM_COMPATIBLE=y` once rustc also uses LLVM 22.

### Readiness criteria

Start the switch only when all of these hold:

1. **bindgen.** A suite DKC already uses (`trixie`, `trixie-updates`,
   `trixie-security`, or `trixie-backports`) offers bindgen 0.72.1 or newer, or
   a 0.71.1 revision whose changelog carries the PR 3278 fix.
   `make build-image LLVM_MAJOR=22` then passes its bindgen probe, and the
   probe output keeps the fields of both forward-declared structs.
2. **The kernel's own check.** For the Sid series being built, the kernel's
   `rustavailable` target, run with `LLVM=-22` in the prepared source tree,
   prints no bindgen/libclang warning.
3. **LLVM packages.** All seven LLVM 22 packages listed above resolve on amd64
   from the `trixie` suite itself (not only from proposed updates) at one
   common version. `make closure-proof` accepts the image.
4. **Kernel series.** The Sid series being built accepts LLVM 22 in
   `scripts/min-tool-version.sh` and has no Clang 22 workaround or warning
   that affects the Debian amd64 configuration.
5. **Qualification.** A complete pull-request qualification passes with
   LLVM 22: both release flavors built with ThinLTO, every package, Kbuild, and
   SIMD audit, KVM boot and selftests, and the package matrix with headers
   installed in clean clients that have no backports configured.

To check criterion 1, query the archive for `bindgen` in the Debian 13 suites
(for example with `rmadison bindgen`) and read the changelog of any new
revision.

### Switch steps

1. Set `LLVM_MAJOR ?= 22` in `mk/container.mk` and `ARG LLVM_MAJOR=22` in
   `container/Containerfile.build`. LLVM then installs from `trixie`, and the
   backports source remains only for `rustc` and `rust-src`. If bindgen comes
   from `trixie-backports`, install it with `-t "${DEBIAN_SUITE}-backports"`.
   Update the toolchain comments at the top of the Containerfile.
2. Regenerate the overlay of every supported source profile with
   `make overlay-patches`, so `0001-select-llvm-toolchain.patch` sets
   `llvm_major = 22` and the headers depend on `clang-22`, `lld-22`, and
   `llvm-22`.
3. Make the client gates prove that backports is no longer needed:
   - `scripts/in-container/test-package-client.sh` stops adding
     `trixie-backports` in headers mode and requires the LLVM tools to resolve
     from `trixie`;
   - `tests/integration/qemu/guest-validate.sh` stops adding
     `trixie-backports` to the guest.
   Keep `trixie-backports` in `scripts/in-container/closure-proof.sh`, because
   the build image still installs `rustc` from it.
4. Rebuild and reconcile the per-series acceptance inputs. Clang 22 code
   generation can change the final-artifact SIMD symbols, so update
   `simd_allowlist` in each `config/source-profiles/<series>/profile.toml` to
   the exact unexpected and stale entries the audit reports, and review the
   Kbuild audit floors in `validation.toml`.
5. Update the user and contributor documentation:
   - the README Clang/LLVM bullet and headers instructions;
   - [docs/USER_INSTALL.md](docs/USER_INSTALL.md): remove the backports section
     and explain that users who already enabled backports can keep or remove
     it, and that `clang-21` becomes autoremovable after the upgrade;
   - the LLVM rows and the client trust path in
     [debian-overlay/COMPATIBILITY.md](debian-overlay/COMPATIBILITY.md);
   - [docs/BUILD.md](docs/BUILD.md) and the troubleshooting row in
     [docs/CLOUDFLARE_CACHE.md](docs/CLOUDFLARE_CACHE.md);
   - the tests in `tests/` that pin these strings.
6. Record the change in `CHANGES.md` as a user-visible dependency change. After
   the first accepted LLVM 22 release, record the new client evidence in
   [docs/VALIDATION.md](docs/VALIDATION.md).

The switch changes the build-policy digest. For a Debian source that is already
published, the production lifecycle therefore reports `blocked` until
`DKC_REVISION` is raised or Debian uploads a newer source; see
[docs/PUBLISHING.md](docs/PUBLISHING.md).
