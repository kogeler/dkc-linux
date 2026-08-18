# CI capacity and artifact budget

Status: `PASS`. Standard public-repository `ubuntu-26.04` is the selected build
executor. Paid GitHub-hosted runners and self-hosted runners are not used.

## Decision

GitHub documents the standard public Linux runner as 4 vCPU, 16 GB RAM, and
14 GB SSD. It also documents a six-hour maximum execution time for a
GitHub-hosted job. The Ubuntu 26.04 image is a public preview, so measured
capacity can differ from the conservative published storage figure.

Two builds on image `20260804.94.1` established the usable allocation. The
accepted GitHub Actions run is `31609392436`. It completed the build, bounded
attestation, Lintian audit, and evidence upload on the standard runner without
OOM or a cgroup limit event.

The project accepts the standard runner when a complete flavor job succeeds
without OOM and within GitHub's hosted-job limit. RAM pressure and headroom are
reported for diagnosis and optimization; they do not turn an otherwise
successful build into a runner-selection failure. This criterion directly
answers the operational question: whether the project can avoid a self-hosted
runner.

## Accepted hosted measurement

| Measurement | Result |
|---|---:|
| Runner CPUs / Kbuild jobs / container CPU quota | 4 / 4 / 4 |
| Physical RAM | 16,759,635,968 bytes |
| Container memory limit | 17,179,869,184 bytes (16 GiB) |
| Raw cgroup memory peak | 13,391,482,880 bytes |
| Reclaim-adjusted working-set peak | 9,204,981,760 bytes |
| `memory.high` / `memory.max` events | 0 / 0 |
| OOM / OOM-kill events | 0 / 0 |
| Build elapsed | 10,090 seconds |
| Build plus attestation and Lintian | 10,223 seconds |
| Complete measured phase through upload | 10,433 seconds |
| Complete GitHub job | 10,539 seconds (2:55:39) |
| Build-related root-filesystem growth | 28,930,301,952 bytes |
| Upload input | 118,679,509 bytes in 50 files |
| Uploaded ZIP | 118,689,167 bytes |

The 16-GiB Podman cap was slightly larger than physical `MemTotal`, so it did
not artificially constrain this run. Raising it would not expose additional
physical RAM. Normal builds therefore use no synthetic memory or CPU cgroup
cap. Kbuild takes its concurrency from `nproc`: four jobs on the verified
standard runner and all available CPUs on a larger local development machine.

The first hosted run, `31586698076`, used a 12-GiB cap. It still completed in
10,195 seconds with no OOM, although file-cache charging caused 736
`memory.max` events. That result already demonstrated practical feasibility;
the accepted rerun confirms that removing the artificial bottleneck eliminates
the events without materially changing build time.

## Disk and cleanup decision

The accepted runner exposed a 154,993,672,192-byte root filesystem and
100,543,447,040 bytes available before cleanup. The temporary experiment
removed 17,393,852,416 bytes of Android, .NET, Homebrew, and vcpkg files, but
the build itself needed only 28,930,301,952 bytes of additional root storage at
peak.

Applying the measured build growth to the pre-cleanup state leaves an estimated
71,613,145,088 bytes available at peak. Cleanup therefore has no operational
value for this workload. Its cleanup privilege exception, image-specific allowlist, runner
inventory, capacity evaluator, and temporary workflow were removed after the
accepted run. Normal CI does not delete preinstalled runner software.

The separate, narrowly scoped virtualization setup in each disposable flavor
job installs QEMU and directly grants the runner process access to an existing
`/dev/kvm` device. The package transaction suppresses the `needrestart` hook:
the hosted VM is discarded after the job, and restarting its agent is neither
necessary nor safe. It deliberately avoids udev rule reloads and synthetic
device events. Unavailable or incompatible KVM fails the unprivileged QEMU
preflight before an expensive v2 or v3 build starts. The setup does not remove
runner content and does not change the disk-capacity conclusion.

This deliberately relies on the allocation observed on two fresh runners,
rather than treating the documented 14-GB figure as the filesystem size the
preview image actually exposes. A real capacity or duration regression, not
loss of an arbitrary percentage margin, is what reopens the self-hosted-runner
decision.

## Product and evidence validation

The accepted build produced six `.deb` files plus one `.buildinfo` and one
`.changes` file. All eight hashes match the build manifest; the checksums inside
`.changes` and `.buildinfo` also validate. All package payload paths are
relative and safe. No debug or installer package was emitted.

The bounded attestation reconciled all 4,225 shipped modules with 32,336 Kbuild
records, inspected 32 deterministic module samples, confirmed LLVM 21.1.8 and
BTF, and preserved a compressed canonical command inventory. The complete
83,541,239-byte build log is XZ-compressed to 1,718,424 bytes and retains the
SHA-256 of its uncompressed contents. Lintian reported only informational and
pedantic findings, with no error.

The main and receipt ZIP digests downloaded from GitHub match the service
receipts. Their 52 combined entries contain no absolute path, `..` traversal,
or symlink. All 91 streamed repository-input hashes match the checkout recorded
by the accepted run.

## Ongoing policy

- Use one fresh standard `ubuntu-26.04` job per release flavor. Let `nproc` select
  Kbuild concurrency: it reports four on this runner and the full available
  count locally.
- Apply no synthetic memory, swap, or CPU cgroup cap to normal build and test
  containers. Retain only the process-count safety boundary and timeouts.
- Do not use paid GitHub-hosted runners.
- Do not delete software from GitHub-hosted runner images.
- Save only a fully qualified flavor handoff in the exact main-branch Actions
  cache, never a build tree. Workflow artifacts remain limited to bounded
  lifecycle/signing evidence and the complete verified repository handed to
  publication.
- Keep complete jobs below GitHub's six-hour maximum; use a project timeout
  that still leaves time to seal an accepted cache entry or retain bounded
  failure evidence.
- Do not configure a self-hosted runner for periodic v4 maintenance; keep that
  work outside the automatic release until the policy in [`TODO.md`](../TODO.md)
  changes.
