# Validation evidence

This file distinguishes implemented repeatable checks from operator acceptance.
A local `PASS` does not claim that a future hosted or production run passed.

## Repeatable repository checks

`make fast` runs the complete Python unit/integration-fixture suite, strict type
checks, Ruff lint and format checks, ShellCheck, shfmt, language policy, and
Make policy in the pinned toolbox image.
`make site` separately builds and audits the documentation site; the `site`
workflow runs it for every pull request. The suite covers:

- signed source discovery and Debian version ordering;
- exact source/state/pool handoff boundaries, repeated OpenPGP state
  authentication, source rollback rejection, and lifecycle decisions over
  downstream revision, build-policy digest, LTO mode, retention policy, and
  measured storage size, including rejection of every contradictory
  decision/flag combination;
- strict storage connection files and bounded output redaction;
- SigV4 canonical requests, pagination, metadata, throttling, bounded read
  retries after transport failures and transient server errors, and conditional
  create/update/delete behaviour;
- lease acquisition, renewal, release, stale takeover, and fencing;
- failure after every publication phase and idempotent convergence;
- immutable reuse without redundant PUT or second body download, conditional
  HEAD validation, pre-commit collision rejection, post-commit object changes,
  and live structured progress alongside buffered secret redaction, including
  real HTTPS socket tests at every interruption boundary;
- two-mode three-series retention, patch-release atomicity, protected newest
  releases, whole-storage byte budgeting, and permanent signed tombstones;
- complete exact GC, liveness, generation, byte, object-count, partial-plan,
  and changed-byte rejection;
- source-bound release-cache identity independent of image rollover, complete
  restored-file verification, exact-key-only restore, tamper rejection,
  run/attempt-isolated pull-request transport, and exact main-ref cleanup;
- publication epochs derived immediately after the authenticated source time,
  future-epoch rejection, complete source-tree mode/mtime normalization, and
  full tar-header validation for the downstream source archive plus both inner
  archives of every binary package, including rejection of stale/future mtimes,
  unsafe or duplicate paths, unstable ownership, special entries, PAX metadata,
  and non-public modes;
- workflow trigger, acyclic dependency graph, declared job-output use,
  producer-attempt artifact routing, unique YAML mappings, secret boundaries,
  cache/KVM, build/maintenance/qualification/no-op convergence, pre-sign
  decision binding, and publication dependency invariants;
- retry-safe content-derived immutable publication namespaces;
- exact predecessor publication binding at decision, signing, and storage-CAS
  boundaries;
- documentation-site page mapping, link rewriting, home-page structure, object
  keys, manifest and schema agreement, delivery-rule agreement, every audit
  rejection, prefix-confined conditional publication, deletion caps, read-back
  verification, and bounded lease waiting.

The full kernel and repository workflow adds the expensive evidence that unit
fixtures cannot provide: real v2/v3 ThinLTO compilation, complete build/SIMD
attestation, KVM boot and selftests, cross-flavor package reconciliation,
clean-client installation and DKMS, exact downstream source reconstruction,
production-key signatures, by-hash acquisition, and final signed-repository
verification.

`make storage-publication-replay` additionally consumes an already verified,
signed generation-zero repository and runs the real publisher twice against a
local HTTPS fixture without external network access. It checks the full saved
payload, signatures, lease, state, evidence checksums, and the exact reuse
transfer budget without compiling kernels or using production credentials.
See [STORAGE.md](STORAGE.md#disposable-qualification).

The hosted build cache is intentionally not evidence by itself. A producer
seals it only after all expensive gates pass, and each consumer recomputes its
key from the authenticated lifecycle decision and validates every cached byte.
Packages, source, replay binaries, and full build logs are not uploaded by a
flavor job. Compact build-audit and VM/selftest reports are copied to a separate
size-bounded artifact with an exact root checksum inventory. A failed APT or
publication stage preserves the accepted cache for retry; the terminal job
removes it after final remote-state verification, while maintenance and no-op
paths repeat the same exact cleanup idempotently.

The pull-request graph is locally accepted by the repository suite, including
its typed non-publishing decision, forced cache-transport miss, storage/secret
exclusions, and DAG/output invariants. A direct `make
github-apt-repository-qualify` run on 2026-08-19 additionally generated a
disposable key, assembled and signed the binary/source repository, passed the
complete clean client and negative signature cases, and recorded
`publishable=false`. The same flow also passed with `GITHUB_ACTIONS=true`,
covering its CI-only signing guard. This was the local prerequisite for the
hosted pull-request acceptance recorded below.

[Pull-request run 32221805371](https://github.com/kogeler/dkc-linux/actions/runs/32221805371)
confirmed authenticated source discovery, the fast tier, the typed
`qualification` decision, and release preflight. It did not provide hosted
build acceptance: GitHub suppressed the flavor matrix because its condition
lacked `always()` while the successful decision had deliberately skipped
production-only ancestors. The workflow now combines `!cancelled()` (originally
`always()`) with explicit success requirements for every direct flavor
dependency; repository structure tests enforce that convergence rule.

[Pull-request run 32225405539](https://github.com/kogeler/dkc-linux/actions/runs/32225405539)
then completed the entire non-publishing graph from real cache misses. Both
ThinLTO flavors compiled and passed package, Kbuild, SIMD, KVM boot, and kernel
selftest qualification; v2 completed in 3h16m and v3 in 2h06m. The dependent
job reconciled 20 inputs into 18 unique packages, passed image and headers/DKMS
clients, assembled the 19-binary/two-source repository, signed it with a
disposable key, and passed the complete clean client and negative signature
cases. Every credential-bearing storage and production-signing job remained
skipped. Inspection of the downloaded APT evidence exposed that its retained
subdirectories still carried full-result checksum manifests referring to
intentionally omitted repository files. The current artifact adapter instead
creates one bounded directory with its own exact root inventory; unit tests
cover direct verification, omissions, tampering, links, and partial-failure
evidence.

## Local Linux 7.3 qualification

On 2026-11-08, Debian `linux` 7.3.3-1 with DKC revision 3 completed the local
non-publishing qualification flow using Clang/LLVM 22.1.8, Rust 1.96.1, and
ThinLTO. Those tool versions also match the active published CI build image.

- `make release-preflight` passed for 7.3.3-1 and for the reviewed 7.1.7-1 and
  7.2.6-1 sources retrieved from Debian Snapshot, preserving both older overlay
  spellings.
- Both release flavors built ten packages and passed package attestation,
  Lintian, all 32,434 saved Kbuild command records, and disassembly of
  `vmlinux` plus all 4,225 shipped modules. Each compiled exactly 93 reviewed
  FPU objects; neither SIMD audit found an unexpected or stale manual entry.
- Exact-source selftests built with the futex atomic-load repair. Both KVM
  guests passed direct installation, initramfs and bootloader integration,
  the new-kernel boot, in-tree and DKMS modules, selftests, package removal,
  and fallback to the stock Debian kernel. The v2 guest was repeated after the
  shared package auditor was corrected.

| Flavor | Selected / executed / passed | Baseline omissions | Nested skips | Failed |
| --- | --- | --- | --- | --- |
| v2 | 33 / 33 / 33 | 2 AVX-dependent programs | 8 | 0 |
| v3 | 35 / 35 / 35 | 0 | 8 | 0 |

The package matrix reconciled 20 inputs into 18 unique packages with identical
common copies. Clean Debian 13 image-only and complete headers/DKMS clients
passed, including source acquisition. The same matrix and client checks also
passed on retained 7.2.13-1 packages with the updated source-template auditor.
Both runs used the operator's private `umask 077`.

`make github-apt-repository-qualify` then assembled and signed the common
repository with a disposable key. The complete client passed APT signature and
by-hash verification, keyring and both release-kernel installations, current
version selection, source acquisition and source-only rebuild, and rejection
of corrupt or absent signatures. The result records `publishable=false`.

Both accepted flavor caches were sealed and independently verified. Compact
flavor and APT evidence were prepared through the GitHub adapters, and all
three artifact-root checksum manifests passed direct verification. This is
local acceptance; it does not claim a hosted run or production publication.

## Real storage acceptance completed locally

Before production activation, the selected empty S3-compatible bucket was used
as disposable test space. The acceptance covered:

- a complete verified repository upload, direct read, metadata/hash check,
  pagination, ETag race, and zero-object cleanup;
- all twelve publication interruption boundaries and idempotent convergence;
- a committed write whose successful response was locally treated as lost;
- a genuine stale-ETag HTTP 412 that remained a failure;
- lease acquire/renew/release and old-holder fencing;
- exact deletion while live objects remained;
- malformed authentication with distinctive endpoint, region, bucket, access
  ID, and secret canaries absent from captured output;
- final whole-bucket authenticated listing with zero remaining objects.

These destructive cases are intentionally not a recurring live CI matrix.
Their discovered behaviours are represented by the repeatable unit and
integration fixtures. Disposable live qualification remains a local make
target for a future backend change.

## Hosted production acceptance

The first canonical-main lifecycle ran against the empty production origin on
2026-08-18. [Run 32106421861](https://github.com/kogeler/dkc-linux/actions/runs/32106421861)
provided the expensive evidence: both exact cache misses built v2/v3 with
ThinLTO, passed complete build and SIMD attestation, booted and exercised the
selected selftests with KVM, reconciled the package matrix, signed the common
repository, passed the clean-client boundary, conditionally published
generation zero, and completed bounded garbage collection. Its final read-only
job did not start because one checkout action pin contained a typographical
error. The publication transaction itself had already committed successfully;
the failed orchestration tail is not described as a successful whole run.

The checkout pin was corrected and a repository-wide action-pin allowlist check
was added. [Run 32125140504](https://github.com/kogeler/dkc-linux/actions/runs/32125140504)
then completed successfully: it authenticated generation zero, selected the
typed `no_op` path without rebuilding or mutating the repository, repeated the
final signed-state check, and deleted both exact release caches.

An independent authenticated whole-origin audit then proved that the bucket
contained exactly the 52 expected objects and no extras: 42 live repository
objects, 24 unique immutable pool objects, signed state/manifest/transaction
records, checksums, and one released production lease. The signed state
identifies Debian source `7.1.8-2`, DKC revision 1, ThinLTO, generation 0, and
publication `20260818-pd16dfbfe2c2291a2-g0`. Every declared path, byte count, SHA-256,
metadata field, signature relationship, and `SHA256SUMS` membership matched.

This accepts the hosted build-to-origin lifecycle. The clean-checkout audit
passed, and the unattended schedule described in
[MAINTAINER_SETUP.md](MAINTAINER_SETUP.md) is configured.

## Public delivery acceptance

The operator-owned endpoint `https://dkc-linux.romancello.net` was accepted on
2026-08-18 without adding a delivery credential or control-plane call to CI.
The live checks proved:

- a proxied repository hostname with active HTTPS ownership and TLS, no public
  alternate origin hostname, and Smart Tiered Cache;
- exact read-back and an idempotent dry-run for the four project-owned rules:
  one strict public-path block rule, two disjoint cache rules, and one
  per-source rate rule;
- valid `InRelease` and detached `Release.gpg` signatures with the published
  archive key, whose downloaded bytes match the tracked keyring;
- `MISS` to `HIT` transitions for canonical metadata, by-hash indexes,
  versioned packages, and a valid-shape `404`, with the intended two-hour or
  one-year edge freshness and unchanged client Cache-Control headers;
- a cached package range returned `206`, the exact 1,024-byte range, and bytes
  matching the signed package;
- plaintext HTTP, `POST`, a query-bearing repository URL, private state, and
  unknown path shapes were blocked before the origin;
- a 45-request burst produced 34 successful responses followed by 11 `429`
  responses, then recovered to `200` after the ten-second mitigation window;
- a clean Debian 13 client authenticated the repository, requested both
  indexes by hash, installed v2 and v3 image/header metapackages using official
  `trixie-backports` for LLVM dependencies, and reconstructed both published
  source packages without an insecure override;
- a separate clean client installed one exact versioned v3 image/header pair
  while both stable metapackages remained absent, proving the documented pinned
  installation path does not silently opt into later releases.

The exact maintained policy and residual cost boundary are documented in
[CLOUDFLARE_CACHE.md](CLOUDFLARE_CACHE.md). This accepts the public data path;
it does not turn delivery configuration into part of the publication protocol.
