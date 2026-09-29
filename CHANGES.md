# Changes

Notable changes to DKC, newest first. The rules for recording a change and for
cutting a version are in [Maintaining this file](#maintaining-this-file).

## Unreleased

No changes yet.

## 0.1.0 - 2026-09-29

The first versioned baseline. It summarizes everything the project implements
at this point rather than listing individual changes.

### Kernel packages

- Rebuilds the newest authenticated Debian Sid `src:linux` for Debian 13
  (`trixie`) on `amd64` without any Sid userspace. See
  [docs/USER_INSTALL.md](docs/USER_INSTALL.md).
- Publishes the `v2` and `v3` flavors, compiled for the x86-64-v2 and
  x86-64-v3 psABI baselines. The `v4` flavor is implemented and kept for
  periodic manual qualification, but it is not distributed. See
  [config/flavors/README.md](config/flavors/README.md).
- Compiles and links the kernel with Debian-packaged Clang/LLVM 21 and LLD. The
  default is ThinLTO.
- Installs every binary in the collision-free `dkc-linux-*` namespace, with
  stable metapackages. Stock Debian kernels stay installable as a fallback.
- Keeps the Debian 13 kernel image layout in `/boot` for `linux-base` 4.12.
  Headers depend on the matching `clang-21`, `lld-21`, and `llvm-21` from
  `trixie-backports`, so DKMS and out-of-tree modules build with the kernel's
  own toolchain.
- Builds the directly installable image instead of going through Debian's
  separate Secure Boot signing stage. DKC kernels therefore do not boot with
  UEFI Secure Boot enforcement enabled.
- `scripts/dkc-cpu-select` checks whether a machine meets a flavor's CPU
  baseline.

### Debian source handling

- Discovers the Sid source from Debian's signed index and passes a hash-bound
  source inventory to every later stage.
- Keeps every assumption about one upstream series in a source profile,
  currently `7.1` and `7.2`. Profile selection is exact and fail-closed. See
  [config/source-profiles/README.md](config/source-profiles/README.md).
- Generates the packaging overlay from anchored edits and applies those edits
  directly. Each committed overlay patch must describe exactly the lines that
  are applied. A newer upload that only moves the context still builds; a
  changed anchor stops it. See
  [debian-overlay/README.md](debian-overlay/README.md).
- Applies the selftest source fixes with their changed lines exact and their
  context relaxed. A fix that the source already carries is recorded as already
  present. See [docs/KERNEL_TESTING.md](docs/KERNEL_TESTING.md).
- Publishes one complete, deterministic downstream source package with every
  binary release.

### Build and validation

- Runs every build and test step through `make` in ephemeral rootless Podman
  containers or KVM guests. Local targets need no `sudo`. See
  [docs/BUILD.md](docs/BUILD.md).
- Records a build identity that covers the Debian source, the build policy, the
  toolchain image, and the LTO mode.
- Each flavor passes the package and dependency audits, a complete Kbuild
  command audit, a disassembly-level SIMD audit, and attestation replay.
- Each flavor then passes KVM boot, the maintained kernel selftest profile, the
  DKMS and module checks, removal, and fallback to the stock kernel.
- Reconciles the packages across flavors, then installs the image and the
  headers in clean Debian 13 clients before signing.
- `make fast` runs the offline unit, type, shell, language, and Make checks.
  `make release-preflight` checks the real source, overlay, toolchain, and
  dependency closure.

### Repository and publication

- Publishes one signed common binary and source APT repository. The primary
  archive key stays offline; CI signs with one protected subkey. See
  [docs/KEYS.md](docs/KEYS.md).
- Publishes to provider-neutral S3-compatible storage with conditional writes,
  lease fencing, signed authoritative state, and idempotent convergence after a
  failure in any phase. See [docs/PUBLISHING.md](docs/PUBLISHING.md) and
  [docs/STORAGE.md](docs/STORAGE.md).
- Retains the newest three upstream series, optionally under a whole-storage
  byte limit. Retired objects get signed tombstones and exact bounded deletion.
  See [docs/RETENTION.md](docs/RETENTION.md).
- Serves the public endpoint through an operator-maintained delivery cache. CI
  holds no CDN credential. See
  [docs/CLOUDFLARE_CACHE.md](docs/CLOUDFLARE_CACHE.md).

### Automation

- An unattended GitHub Actions lifecycle checks Debian four times a day. It
  builds, tests, signs, and publishes a new source version, and treats an
  unchanged one as a no-op. Production runs only from canonical `main`. See
  [.github/workflows/README.md](.github/workflows/README.md).
- Pull requests run the complete build and qualification graph without
  production secrets and never publish.
- Sealed release caches let a failed later stage retry without rebuilding. Each
  flavor uploads a compact, checksummed evidence artifact.

## Maintaining this file

- Record every change that users, operators, or contributors can notice as one
  entry under `## Unreleased`, in the same change that makes it. Noticeable
  changes include packages and installation, repository contents or signing,
  the lifecycle and CI behavior, make targets, source profiles and the overlay,
  build identity, and validation or acceptance rules. Refactoring,
  test-only changes, and typo fixes need no entry.
- Group entries under `### Added`, `### Changed`, `### Fixed`, `### Removed`,
  and `### Security`, in that order, and omit empty groups. `0.1.0` is the one
  exception: it is a baseline summarized by area.
- Write each entry as one or two plain English sentences that say what changed
  and whom it affects. Link the document that holds the details instead of
  repeating them.
- Keep entries self-contained. Do not name private plans, workflow run numbers,
  secrets, non-public endpoints, account identifiers, bucket names, or storage
  providers.
- The project version follows [Semantic Versioning](https://semver.org/). It is
  independent of the kernel package versions, which follow the Debian source
  version plus the DKC revision.
  - A patch version adds only fixes and robustness improvements that keep every
    documented contract.
  - A minor version adds a capability, a supported upstream series, or a flavor.
    Below `1.0.0` it also carries any incompatible change.
  - From `1.0.0` on, a major version carries an incompatible change to the
    installed-system contract: the repository location, suite, or key; package
    names; or the supported Debian release.
- Cutting a version is an operator decision. Rename `## Unreleased` to
  `## X.Y.Z - YYYY-MM-DD` and start a new `## Unreleased` above it. The
  operator may then tag that commit `vX.Y.Z`.
- Edit a released section only to correct a factual error.
