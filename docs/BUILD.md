# Building DKC

All repeatable operations start at `make`. They require an unprivileged user,
rootless Podman, and no `sudo`. The source-staging and package-client phases use
the network; compilation, packaging, Kbuild attestation, and final SIMD scanning
run with no network interface other than loopback.

## Container images

Local Make invocations build the image they need from the tracked Containerfile.
Podman layer caching remains enabled, so repeating an unchanged invocation
reuses the local package-install layers:

```sh
make image
make build-image
make apt-client-image
make container-images
```

`container-images` treats the toolbox, kernel build environment, and minimal
APT client as one bundle. Its inputs are listed in
`config/container-images.inputs`; a deterministic fingerprint covers the
content inputs, pinned Debian base, and LLVM major. Local mode is the default
and requires no registry credentials.

GitHub Actions explicitly selects registry mode. The image lifecycle is
independent from the kernel lifecycle: the resolver takes the current three
public `latest` tags, requires one coherent publication generation, and gives
later jobs immutable `@sha256:<manifest-digest>` snapshots of those tags. It
does not compare them with the kernel checkout or make their digests part of a
release-cache key. Registry mode rejects mutable references after resolution
and cannot execute a Containerfile build. A flavor result separately records
the registry manifest digest, local image config digest, bundle fingerprint,
and publication generation in `evidence/build-image-provenance.env`.

The published image names are:

```text
ghcr.io/kogeler/dkc-toolbox:latest
ghcr.io/kogeler/dkc-kernel-build:latest
ghcr.io/kogeler/dkc-apt-client:latest
```

Only `latest` is a supported tag. The immutable digest references produced by
the resolver are consumption identities, not additional version tags.

## Fast gates

Build the pinned toolbox and release-build images, then validate the repository
and the real Debian packaging overlay:

```sh
make image build-image
make fast
make release-preflight
```

`release-preflight` downloads and extracts the pinned Debian source once, then
checks the complete packaging overlay, toolchain wiring, generated package
graph, and build-dependency closure on that same tree. It therefore needs
network access. `make fast` is offline once the toolbox image exists.

## One flavor

```sh
make build-flavor FLAVOR=v2
```

Valid flavors are `v2`, `v3`, and `v4`. `BUILD_JOBS` defaults to `nproc` inside
the invoking environment. Normal local and GitHub builds have no synthetic
Podman CPU, memory, or swap cap: a four-CPU hosted runner naturally selects four
jobs, while a larger development machine uses its available CPUs.

The networked source stage does not reconstruct its build environment from a
moving Debian mirror. The immutable build image retains the `.deb` archives
downloaded for packages added or upgraded on top of its digest-pinned base.
Staging verifies their installed package identities and hashes those bytes;
packages inherited unchanged from the base are covered by the base digest.
The installed package/version inventory and image provenance remain separate
evidence.

## Release flavor policy

The automatic CI and binary distribution contain `v2` and `v3`. The complete
`v4` implementation remains a supported manual build target; its configuration,
package generation, command audit, final machine-code SIMD attestation,
selftest build, and QEMU paths are deliberately kept in the source tree.

This distinction follows from how Linux uses SIMD. Ordinary kernel C and Rust
compilation reasserts the reviewed no-SIMD controls after the selected
`-march`/target CPU. AVX-512 appears instead in explicitly optimized crypto,
RAID6, and checksum implementations. Linux compiles those implementations into
the lower flavors too and selects them at runtime after checking CPU and
extended-state support. A `v3` kernel can therefore execute the same AVX-512
hot paths when it boots on a capable processor.

A complete static encoding scan of retained builds found 2,342 AVX-512
instructions in both a `v3` ThinLTO build and a `v4` FullLTO build, with the
same distribution across 27 symbols in five artifacts. That is 0.00696% and
0.00642% of their respective decoded instruction populations. Static counts
do not predict dynamic workload frequency, but the identical runtime-dispatched
implementations show that a separate v4 package is not needed to make those
paths available. Any residual scalar difference caused by the compiler target
is expected to be small and has not justified a third automatic build, a
stricter installation baseline, or an unqualified binary release.

Periodic v4 maintenance is performed explicitly on a machine that exposes the
complete baseline:

```sh
make build-flavor FLAVOR=v4
make kselftest-flavor FLAVOR=v4
make qemu-boot-flavor FLAVOR=v4
```

The release package matrix intentionally ignores that result. Re-enabling v4
distribution requires restoring it to the CI matrix and the explicit release
flavor lists in the package-matrix, repository, and client gates; it must not
be enabled merely because a package compiles.

Link-time optimization is an independent build policy, not a CPU flavor or a
package-name component. Clang ThinLTO is the default. Disable LTO or select
FullLTO explicitly:

```sh
make build-flavor FLAVOR=v3 KERNEL_LTO=none
make build-flavor FLAVOR=v3 KERNEL_LTO=full
```

Valid `KERNEL_LTO` values are `none`, `thin`, and `full`. The selected value is
embedded in the reconstructible source policy and publication identity, and
the final and shipped configurations plus every normal kernel C compile command
are checked for the corresponding setting and `-flto` mode. Package roles and
the `v2`/`v3`/`v4` flavor names do not change. As with any byte-affecting input,
the existing build ID changes. For an experiment that must not replace local
stable-result pointers, pass `UPDATE_LATEST=0`; the run remains available by
its explicit ID.

Linux 7.1 does not permit Rust, BTF, and LTO together. The project keeps Rust
mandatory: `none` therefore requires BTF and BTF modules, while `thin` and
`full` explicitly disable both BTF settings. This choice is part of the
resolved policy configuration and is checked again in the shipped package;
LTO can never silently change either Rust or BTF policy.

The Debian backports Rust compiler is used because kernel LTO links Rust and C
LLVM bitcode together. Image construction rejects a Rust compiler whose LLVM
major differs from the selected Clang major; without that equality Kconfig
would disable Rust rather than produce a mixed-LLVM LTO build.

Every invocation independently verifies and stages the same Debian source and
complete build-dependency byte lock. Both the normalized package/version/hash
lock and the authenticated staging-index hashes are exported as evidence. It
preserves each authenticated source member's exact archive filename together
with its URL, size, and SHA-256; neither staging nor the offline build derives
filenames from a previously known kernel version. It then resolves and records
the complete normalized Kconfig for all three
flavors, derives one publication-wide identity from their hashes, selects one
flavor, builds its exact package subset, and runs:

- source-declared Rust/bindgen/LLVM minimum checks and a byte-identical
  preflight-versus-Debian final configuration check;
- final configuration, package, BTF, LLVM, and Kbuild-command attestation;
- exact `.buildinfo`/`.changes` binary inventories and SHA-256 reconciliation;
- exact C/Rust baseline and no-SIMD command-policy checks;
- streaming disassembly of `vmlinux` and every shipped module;
- Lintian with errors fatal.

An accepted result is retained at `out/flavors/<flavor>/<run-id>/`; the
`out/flavors/<flavor>/latest` symlink selects it without copying multi-gigabyte
build trees. The result contains final `.deb`, `.buildinfo`, and `.changes`
artifacts plus bounded evidence. Build trees are deliberately destroyed after export.
Failures in source staging, identity staging, offline build, or final export
retain the evidence produced up to that phase under the corresponding run ID.

Compilation output is not disposable until its post-build gates are
replayable. Immediately after `dpkg-buildpackage` succeeds, every flavor result
therefore records an attestation replay set before any gate can terminate the
run. It contains a debug-stripped `vmlinux` whose every executable section was
byte-compared with the original, `System.map`, the final configuration, the
complete Kbuild command inventory, the symbol inventory derived from all 66
reviewed FPU objects, and the complete SIMD/FPU observation inventory. Shipped
modules remain in the retained `.deb` files. The package payload, BTF,
toolchain, header ABI, and Kbuild reconciliation gates are replayed from the
same inputs. This is sufficient both to re-evaluate SIMD policy without
disassembly and to repeat the complete package and final-ELF attestation
without compiling the kernel:

```sh
# Full vmlinux and all-module rescan; this is the default.
make reattest-flavor FLAVOR=v3 \
  REATTEST_RESULT="$PWD/out/flavors/v3/<run-id>"

# Fast policy replay over the complete retained observation inventory.
make reattest-flavor FLAVOR=v3 REATTEST_MODE=observations \
  REATTEST_RESULT="$PWD/out/flavors/v3/<run-id>"
```

Reattestation never mutates the original flavor result. Its reports are written
under `out/reattest/<flavor>/<run-id>/`; the original result's complete evidence
checksum manifest is verified before any retained input is trusted. Final build
acceptance itself performs
the package and observation replay and requires them to reproduce the primary
package, Kbuild, and SIMD evidence byte-for-byte, so a supposedly replayable
result cannot be exported without exercising the replay path.

If compilation and lintian completed but a package, Kbuild, or SIMD
attestation gate failed, fix the attestor and recover a new immutable accepted
result from the complete retained observations and final ELFs:

```sh
make recover-flavor-attestation FLAVOR=v3 \
  RECOVER_RESULT="$PWD/out/flavors/v3/<failed-run-id>"
```

This recovery accepts only an `offline-build` failure with checksum-valid
packages, source, replay inputs, the original build image, and a passing
lintian result. It replays package, Kbuild, and SIMD policy in a fresh offline
container, then runs the ordinary finalizer. The original failed result remains
unchanged and the kernel is not compiled again.

If compilation and every post-build gate passed but the final host export
failed, recover the immutable export from its checksum-valid failure result;
the kernel is not compiled again:

```sh
make recover-flavor-export FLAVOR=v3 \
  RECOVER_RESULT="$PWD/out/flavors/v3/<failed-run-id>"
```

Final-export recovery is deliberately narrow: it accepts only `final-export` failures,
requires the original build image, packages, source bundle, complete evidence
manifest, zero gate return codes, and replayable reports, then runs the same
finalizer in a fresh offline container. Failures before completed compilation
and replay capture cannot be promoted.

## Complete local release matrix

The local pre-QEMU gate is sequential so all artifacts remain on one machine:

```sh
make build-image
make build-flavor FLAVOR=v2
make build-flavor FLAVOR=v3
make package-matrix
```

`package-matrix` consumes the two release `latest` results by default. It first
requires byte-identical publication identities and exactly ten packages from
each flavor job. It then proves the two copies of each common package have
identical full-file SHA-256 values, selects the `v2` copies as the deterministic
canonical objects, and validates the resulting 18 release binary package names,
the exact internal dependency graph and
maintainer-script lifecycle, matching attested hashes, safe payload entries, no
cross-package file collision, and the reviewed critical path ownership. A
compressed per-package payload inventory is written as evidence.

The result uses two linked checksum scopes. `evidence/evidence.sha256` covers
exactly the compact lifecycle reports and both clean-client results, so the
diagnostic workflow artifact remains independently verifiable after download.
It also binds `evidence/flat-repository.sha256`, which covers the much larger
binary/source input directory consumed by repository assembly. The assembler
requires both exact manifests. This avoids uploading a duplicate copy of all
packages while preserving a complete integrity boundary before signing and any
future external publication.

It then creates clean Debian 13 clients. The image-only client installs the two
release image meta-packages and proves that no compiler enters their dependency
closure. The headers client installs the complete union of all 18 DKC `.deb`
files in one dpkg database and verifies:

- stock Debian plus v2/v3 kernel coexistence and initramfs generation;
- exact `/boot` and `/lib/modules/<KREL>` layout;
- conventional `/usr/src/linux-headers-*` payloads and exact `build`/`source`
  symlink targets despite the DKC package namespace;
- versioned LLVM tools from Debian backports;
- plain out-of-tree and controlled DKMS builds for every KREL, their real build
  commands, and matching vermagic;
- isolated v3 removal without damaging v2 or the stock kernel;
- downloadable allowed origins for every installed package, plus APT
  meta-package retention and exact v3 autoremove behavior.

Before compiling, every flavor job also creates the same normal Debian
`dkc-linux` source bundle: `.dsc`, original tar, Debian tar, source `.changes`,
and source `.buildinfo`. The build then starts from the tree reconstructed by
that `.dsc`. Every independent flavor export carries its verified physical
source bundle so a following test-only task can run without a cross-job
dependency. The matrix rejects any disagreement,
publishes one source copy beside the 18 release binary packages, and checks
that every binary names the same source and version.

The downstream source bundle still declares the complete 26-package v2/v3/v4
build graph. This is intentional: public source retains the dormant v4 build
capability, while the binary archive admits only the 18 packages selected by
the release matrix.

After the package matrix passes, a separate no-secret stage creates one common
repository with standard `pool/` and `dists/trixie/` layouts. It contains the
18 release kernel packages, an independently reconstructible `dkc-archive-keyring`
source/binary package, and both source packages in `Sources`. `Release` uses a
14-day `Valid-Until`, advertises `Acquire-By-Hash: yes`, and binds all six
`Packages*`/`Sources*` representations with SHA-256. A strict signing request
binds every accepted path, size, and digest. The keyring package version and
source timestamp derive only from the active public subkey and public-bundle
digest, so retrying repository assembly cannot create different package bytes
under one version. Per-invocation `.buildinfo` and `.changes` files remain in
the unsigned job's bounded evidence; they are not stable `deb-src` objects and
therefore do not enter the signed APT `pool/`.

The production signer receives only that immutable handoff, the tracked public
certificate, and an isolated secret signing subkey. It proves that no primary
secret is available, selects the exact active subkey, and emits a bounded
signature/state overlay. A final no-secret stage rejects any extra, missing,
or replaced file before merging the overlay. A fresh client derived from the
pinned Debian 13 image then verifies `InRelease`, `Release.gpg`, root checksums,
immutable state and transaction signatures; performs `apt update` through real
SHA-256 by-hash requests; installs both release kernel image meta-packages and
verifies their boot, initramfs, and module payloads; extracts both source
packages; rebuilds the `dkc-linux` source archive and compares all reconstructed
source entries; installs the archive keyring package and updates again through
that installed key; and rejects corrupted and unsigned metadata. Networking is
disabled and no insecure APT option is used. The verified repository and
bounded evidence are retained as seven-day workflow artifacts. Only that
verified repository artifact can enter the following conditional storage
publication job; signing by itself never authorizes an external write.

Locally, the same assembly, signing, and verification entry point can generate
disposable keys when explicitly requested:

```sh
DKC_APT_EPHEMERAL_SIGNING=1 make apt-repository
```

That mode is rejected in GitHub Actions. Production key provisioning and the
GitHub Environment boundary are documented in [`KEYS.md`](KEYS.md).

## Virtual-machine validation

Build the test bundle separately, then pass both accepted results to the VM:

```sh
make kselftest-flavor FLAVOR=v2
make qemu-boot-flavor FLAVOR=v2
```

The test-only target reconstructs the accepted source package, verifies the
selected flavor's shipped configuration, applies exact-context selftest
compatibility patches, and compiles no kernel package. Its report binds the
profile, patch manifest, source identity, shipped configuration, and output
bundle hashes. The VM copies the flavor result's ten attested `.deb` files,
the separate selftest bundle, and a controlled DKMS fixture
onto a read-only input ISO. The clean Debian 13 guest verifies their hashes,
unpacks and configures them directly with `dpkg`, boots the exact kernel,
checks modules, networking, storage, and compiler identity, then runs the
curated exact-source kernel selftest profile. No APT repository, package index,
archive key, or signature is created for this VM step; APT is used only for
ordinary Debian prerequisites absent from the base image. The guest finally
returns to the stock kernel, purges the tested kernel packages directly with
`dpkg`, and verifies bootloader cleanup. The complete profile has per-test and
aggregate time limits and retains a compact summary plus compressed TAP and
serial logs.

The profile contains 35 explicit interfaces. The v2 guest executes 33 and
records two baseline omissions for AVX/OSXSAVE-specific x86 tests; v3 and v4
execute all 35. The guest temporarily lowers an unusually large `fs.nr_open`
while the upstream EMFILE scenario runs and restores the original value. A
small wrapper selects the passing `/proc/PID/mem` portion of the upstream
ptrace executable, while an exact-context source patch enlarges the uevent
listener buffer to prevent a known `ENOBUFS` failure. These baseline omissions
are separate from runtime `SKIP`
results. The summary also reports program-level and nested subtest skips
separately, so a missing feature is never reported as a fully exercised test.
The complete rationale, exact environment-sensitive fixtures, retained
evidence, and rules for changing this selection are documented in
[`KERNEL_TESTING.md`](KERNEL_TESTING.md).

VM qualification requires KVM. There is no software-emulation fallback: an
absent or inaccessible `/dev/kvm`, a missing QEMU KVM accelerator, or an
unsupported requested CPU feature fails preflight. Local make targets never
invoke privilege escalation. The GitHub workflow has a narrowly scoped setup
step on its disposable VM to install QEMU, make `/dev/kvm` accessible, and
preflight the exact flavor model before starting the expensive build.

The target downloads the immutable Debian 13 cloud image named in
`config/qemu-image.env`, verifies its pinned SHA-512, and keeps it only in the
declared cache. The same one-flavor target uses the CPU models in
`config/qemu-cpus.env`. Hosted CI invokes it for both release flavors. The v4
path remains available for periodic manual qualification on a capable host as
tracked in [`TODO.md`](../TODO.md). VM resources default to two
vCPUs and 4096 MiB because these are guest device assignments, not synthetic
limits on a kernel build.

Each flavor starts from a new 16 GiB copy-on-write overlay of the same stock
image. The guest installs the exact attested input files directly with `dpkg`,
validates CPU selection, initramfs and GRUB hooks, boots the exact release,
loads an in-tree and DKMS module, reboots the stock fallback, and removes all
DKC kernel packages directly. The overlay and raw result disk are destroyed
after the result files have been extracted. Compact guest
reports, the exact QEMU command, hashes, and compressed serial log remain under
`out/qemu-boot/<run-id>/`.

The current package gate does not yet claim an upgrade between two different
DKC revisions or an independent
reproducibility rebuild; those fields remain `NOT_RUN` until those executions
actually exist.

## GitHub Actions

A separate `container images` workflow owns the complete image bundle. It
builds and publishes all three images together after a relevant push to
canonical `main`, at 09:00 UTC each Saturday, or from a manual run whose ref is
the current `main`. A relevant pull request builds and verifies all three but
has read-only permissions and never authenticates to the registry. The
publisher uses only its job-scoped `GITHUB_TOKEN`, has no delete permission,
and verifies anonymous pulls after publishing.

The main workflow never rebuilds these Containerfiles. A separate read-only
workflow checks candidate image changes in pull requests. A trusted run waits
for an already active main image publication to finish, then snapshots whatever
coherent `latest` bundle is current; it never waits for a checkout-derived image
fingerprint. Partial updates to the three mutable tags therefore cannot create
a mixed build environment, while the image and kernel schedules remain
independent. Pull-request kernel jobs use that same published bundle, never the
unpublished candidate images built by the separate path-filtered image workflow.

Trusted lifecycle triggers first discover authenticated Debian source and read
signed repository state. The typed decision builds only when the source is new,
the downstream revision was deliberately increased, or an explicitly allowed
empty bootstrap is required. A current source reaches metadata maintenance or a
no-op without compiling a kernel. A manual dispatch from any branch or tag
other than current `main` fails before image resolution or compilation.

For a production build decision, v2 and v3 run on independent standard
`ubuntu-26.04` runners. Each job computes one semantic Actions cache key from the
authenticated Debian source, downstream revision/build policy, flavor, and LTO
mode, plus the tracked validation policy for attestation, selftests, and QEMU.
Image digests remain provenance and do not invalidate an accepted result. A
fully verified exact hit skips QEMU setup, compilation, selftest construction,
and VM execution. There is no prefix restore or compatibility path. On a miss,
the job requires its CPU model to pass KVM preflight,
runs `make build-flavor`, `make kselftest-flavor`, and
`make qemu-boot-flavor`, and seals the result only after every gate passes.
The dependent package job restores both entries, verifies every cached byte and
semantic identity again, and then runs `make package-matrix`. Flavor packages,
source, replay payloads, and full build logs are not uploaded as workflow
artifacts. Each flavor does upload a bounded, self-verifying report bundle with
capacity, Kbuild/SIMD/package attestation, selftest summaries, and VM evidence;
it contains no packages. The exact caches remain available after a downstream
failure and are deleted by the terminal job only after the intended signed
repository generation is read back successfully from storage. Artifact
consumers use producer-exported names, so
a failed-jobs-only retry reuses the successful producer attempt instead of
searching for a nonexistent artifact under the new attempt number.

A pull request targeting `main` uses the same authenticated source, release
preflight, v2/v3 build, SIMD/Kbuild/package attestation, KVM boot, selftests, and
package matrix. Its decision is a separate `qualification` state with
`build_required=true` and `publish_allowed=false`. The semantic identity is
still checked, but the cache transport key includes the workflow run and
attempt, guaranteeing a miss even though GitHub permits pull requests to read
default-branch caches. The package job restores only those attempt-local
handoffs. It then signs a newly assembled repository with a disposable key and
runs the complete clean APT client. Authoritative state, the production signing
key, external publication, and final state verification are never reachable
from this decision.

There is no cache prefix fallback, paid-runner selector, runner cleanup, or
synthetic CPU/RAM cap. Local sequential builds do not populate or consume the
GitHub release cache.
