# Kernel test profile

This document defines the short kernel-runtime test gate, explains its unusual
fixtures, and records the evidence required before changing it. It is the
maintainer reference for `config/kselftest.env`, the selftest-only build task,
and the QEMU guest runner.

## What the gate proves

The gate complements package, configuration, toolchain, and instruction-set
audits. It boots each produced kernel and exercises a bounded selection of
userspace-facing kernel interfaces across process creation, namespaces,
credentials, file descriptors, filesystems, memory management, IPC, futexes,
signals, seccomp, proc, pidfd, ptrace, timers, networking, and x86 state.

The framework is Linux `kselftest` from the exact kernel source used to build
the packages. This avoids combining a kernel with an independently moving test
release. It is a broad smoke and integration gate, not a replacement for the
complete upstream test matrix, fuzzing, stress testing, hardware labs, or a
long regression suite.

Each enabled flavor VM also verifies the package lifecycle: direct `.deb`
installation, initramfs and bootloader integration, boot of the exact release,
in-tree and DKMS modules, stock-kernel fallback, package removal, and bootloader
cleanup. The VM does not construct or test an APT repository and does not use
an archive key. Repository verification is a separate container test. CI runs
this gate for the v2/v3 release set. The v4 path remains implemented for
periodic manual qualification as described in [`TODO.md`](../TODO.md).

## Build and execution split

Kernel package compilation and test compilation are deliberately separate:

```sh
make build-flavor FLAVOR=v2
make kselftest-flavor FLAVOR=v2
make qemu-boot-flavor FLAVOR=v2
```

`make build-flavor` exports the accepted binary packages, their evidence, and a
complete Debian source bundle. `make kselftest-flavor` then:

1. verifies the accepted flavor result manifests and requires its physical
   source bundle;
2. verifies that the packages, source, build report, and kernel configuration
   have the same publication identity;
3. reconstructs the source through its `.dsc`;
4. extracts and hashes the shipped `/boot/config-<release>` from the accepted
   base package;
5. applies the reviewed exact-context test-source patches;
6. builds every collection named in `DKC_KSELFTEST_TARGETS` with
   `FORCE_TARGETS=1` and installs a portable test tree;
7. binds the source, configuration, profile, patch manifest, file manifest,
   and bundle hashes into the selftest report.

The task compiles UAPI headers and userspace test programs only. It never
rebuilds a kernel package. A profile, wrapper, or test-fixture change therefore
requires a new selftest bundle and QEMU run, but not another expensive kernel
build. The accepted test result is written below
`out/kselftest/<profile-kind>/<flavor>/`, independently of the package result.

The VM input preparation rejects a bundle whose flavor, kernel release, build
identity, LLVM version, or shipped configuration differs from the selected
package result. Tests are compiled before VM creation; the guest only verifies
and extracts the immutable bundle.

## Current qualification profile

`config/kselftest.env` is the authoritative machine-readable selection. The
current profile has 25 mandatory-build collections and 35 explicit
`collection:test` selectors. The complete collection directories are present
in the portable tree, but the guest runs only those selectors.

The profile uses two independent limits:

- 180 seconds for one selected program;
- 900 seconds for the complete profile.

A timeout is a failure, not a skip. A collection omitted by the build is also a
failure. Benchmarks, destructive tests, unbounded stress, nested virtualization,
hardware-specific collections, and tests requiring a large external topology
are outside this short gate unless a concrete coverage gap justifies adding
them.

The `v2` VM records `x86:corrupt_xstate_header_64` and `x86:avx_64` as baseline
omissions because they require AVX and OSXSAVE, which begin at the `v3`
baseline. They remain compiled into the bundle. The `v3` and `v4` guests run
all 35 selectors. An architectural omission is distinct from an upstream
runtime `SKIP` and appears separately in `kselftest-summary.env`.

Some selected programs contain optional nested cases. The current environment
can legitimately report nested skips for unavailable huge pages, a
CPU-count-dependent futex case, a libnuma-version-dependent futex case, and
Landlock host-filesystem layouts that the fixture does not provide. The runner
counts these separately and retains their exact reasons in
`kselftest-skips.log`; a successful outer program must not erase them.

## CPU models and acceleration

The VM must expose the flavor baseline named in `config/qemu-cpus.env`; merely
booting on the host CPU is not sufficient. The preflight requires KVM to
instantiate the exact model with QEMU's `enforce=on`, and the real boot command
keeps the same enforcement. An absent or inaccessible KVM device, a missing
accelerator, or an unsupported requested feature fails the gate immediately.
Software emulation is not accepted for release qualification.

Named QEMU models can request features outside the psABI level they are meant
to represent. The selected `Nehalem-v1` and `Haswell-v2,-pcid` models omit
`spec-ctrl`, which is a runtime mitigation capability rather than a v2 or v3
baseline requirement. The produced kernel still detects and uses that feature
when real hardware exposes it. A server model may likewise add unrelated
paging, mitigation, or vendor-specific flags; preflight treats every rejected
requested feature as a model mismatch rather than weakening the virtual CPU.

The guest records the accelerator and CPU model, verifies the running baseline,
and checks that the compatibility selector rejects higher flavors on lower
models. The default two vCPUs and 4096 MiB are VM device assignments for the
runtime gate; they are not CPU or memory caps on kernel package compilation.

## Environment-sensitive cases

Three selectors need special handling. These decisions came from repeated
candidate-versus-stock runs in the same guest, inspection of the exact test
source, and bounded process-state evidence. They must not be simplified into
unconditional skips.

### `core:unshare_test`

The upstream `unshare_EMFILE` scenario reads `fs.nr_open`, raises it by 1024,
raises `RLIMIT_NOFILE`, creates a descriptor just beyond the old ceiling, then
restores the old sysctl in a child and expects `unshare(CLONE_FILES)` to fail
with `EMFILE`.

The generic cloud image sets `fs.nr_open` close to one billion. Using that
value makes the test request an impractically large descriptor table; the raw
test failed at `dup2()` on both the stock and candidate kernels. This was a
guest-fixture problem, not evidence of a flavor defect.

Immediately before the profile, the guest records the original sysctl and its
inherited hard `RLIMIT_NOFILE`, requires the hard limit not to exceed the
sysctl, and temporarily sets `fs.nr_open` equal to that hard limit. This keeps
the test bounded while preserving an important Linux invariant: an unchanged
hard resource limit must not be greater than the global ceiling. The test can
still raise both values by 1024 as designed. The runner restores and verifies
the original sysctl after the profile, including after a nonzero test status.

Do not lower `fs.nr_open` to an arbitrary smaller number. An earlier 65,536
fixture left the inherited hard limit at 524,288. That caused two unrelated
`close_range_test` cases to fail when they changed only the soft limit, because
their unchanged hard limit had become invalid relative to the sysctl.
`kselftest-nr-open.env` records the original value, temporary value, inherited
hard limit, and successful restoration.

### `ptrace:vmaccess-only`

The installed upstream `ptrace/vmaccess` executable contains two independent
subtests. The `vmaccess` case checks access to `/proc/PID/mem` across a dying
process and passed repeatedly on both kernels. The `attach` case timed out
repeatedly on both kernels even with `kernel.yama.ptrace_scope=0`.

Process snapshots for the timed-out case were the same on both kernels: the
attaching process was blocked in `ptrace_attach`, the target leader was blocked
in `begin_new_exec`, and a prior target thread named the attaching process as
its tracer. This is not useful as a bounded candidate-versus-stock regression
signal.

`tests/integration/kselftest-wrappers/ptrace-vmaccess-only` executes the
upstream binary with `-t vmaccess`. The original binary remains in the attested
bundle; only the independently deadlocking subtest is omitted. Do not replace
the wrapper with a success stub, turn its timeout into a skip, or remove other
ptrace coverage. Revalidate the full upstream executable after a kernel-source
update that changes this test or the exec/ptrace interaction.

### `uevent:uevent_filtering`

The pinned test source used a 4 KiB netlink receive buffer. Under sufficient
uevent activity, `recvmsg()` returned `ENOBUFS`; extending the test timeout did
not repair packet loss. The failure was therefore in the test's receive
capacity, not a slow kernel assertion.

Linux upstream fixed this exact problem by increasing the buffer to 1 MiB in
[commit c7fdbc2c2f26](https://github.com/torvalds/linux/commit/c7fdbc2c2f26b9c397eb3aad2fdc54dbd85f68e1).
`tests/integration/kselftest-patches/0001-uevent-receive-buffer.patch` carries
that one-line change for the pinned source. The corrected test passed repeated
runs on both the stock and candidate kernels.

The build applies the patch with zero fuzz and records its SHA-256 in
`kselftest-source-patches.sha256`. A source update that has already incorporated
the fix, or changed the surrounding code, must fail the old patch application.
Review the new upstream source, remove or replace the patch deliberately, and
repeat the stock/candidate check. This patch is derived from Linux and follows
the per-path license policy in `LICENSES/README.md`; the root MIT license does
not relicense it.

## Rules for changing the profile

Never remove a selected test merely because it failed or took longer than
expected. Before changing the selection:

1. retain the failing selector's TAP, per-test log, kernel log, sysctls,
   resource limits, capabilities, CPU model, and relevant process states;
2. reproduce it on the candidate kernel and the stock guest kernel with the
   same userspace and VM settings whenever the interface exists on both;
3. inspect the exact test source, configuration requirements, and upstream
   changes;
4. repair a safe deterministic guest or harness prerequisite first;
5. rerun the affected case repeatedly, then rerun the complete profile;
6. omit a case only when evidence shows it is destructive, inherently
   unbounded, impossible on the flavor baseline, or unsuitable for a short
   deterministic gate;
7. preserve equivalent subsystem coverage and document the exact reason here.

An upstream test patch is acceptable only when it fixes the test fixture rather
than weakening the behavior under test, has reviewable provenance, applies to
the pinned source without fuzz, is included in the evidence manifest, and is
validated on both stock and candidate kernels. A local wrapper must invoke a
real named upstream subtest and must remain visible in the profile and bundle
manifest.

After any profile, wrapper, patch, guest-limit, or runner change, build a fresh
selftest bundle for each affected release flavor and run every enabled
one-flavor QEMU target. Static profile checks alone are not runtime evidence.
Periodic manual v4 maintenance follows the separate policy in
[`TODO.md`](../TODO.md).

## Evidence to inspect

A successful selftest-only build contains `kselftest-build.json`, the exact
profile, target and selector lists, source-patch manifest, installed-file
manifest, compressed build log, portable bundle, and an evidence checksum
manifest. The QEMU result contains:

- `kselftest-summary.env` for selected, omitted, planned, executed, passed,
  skipped, nested-skipped, and failed counts;
- `kselftest-nr-open.env` for the temporary global-limit transaction;
- `kselftest-failures.log` and `kselftest-skips.log` for bounded details;
- compressed raw TAP and per-test logs;
- the exact QEMU command, CPU model, accelerator, serial log, package lifecycle
  reports, and outer evidence checksums.

Acceptance requires matching planned and executed counts, zero failed selected
programs, no unreviewed omission, verified restoration of modified guest state,
and a complete package/boot/fallback/removal lifecycle. A QEMU process exiting
normally is not sufficient when the guest result says `FAIL`.

GitHub retains these compact reports with the corresponding capacity,
attestation, Kbuild, and SIMD summaries in one flavor evidence artifact. Run
`sha256sum --check evidence.sha256` from the downloaded artifact root before
inspection. Kernel packages, source, replay binaries, and full build logs are
deliberately excluded from that artifact.
