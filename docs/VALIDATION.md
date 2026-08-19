# Validation evidence

This file distinguishes implemented repeatable checks from operator acceptance.
A local `PASS` does not claim that a future hosted or production run passed.

## Repeatable repository checks

`make fast` runs the complete Python unit/integration-fixture suite, type checks,
ShellCheck, shfmt, language policy, and Make policy in the pinned toolbox image.
The suite covers:

- signed source discovery and Debian version ordering;
- exact source/state/pool handoff boundaries, repeated OpenPGP state
  authentication, source rollback rejection, and lifecycle decisions over
  downstream revision, build-policy digest, LTO mode, retention policy, and
  measured storage size, including rejection of every contradictory
  decision/flag combination;
- strict storage connection files and bounded output redaction;
- SigV4 canonical requests, pagination, metadata, throttling, and conditional
  create/update/delete behaviour;
- lease acquisition, renewal, release, stale takeover, and fencing;
- failure after every publication phase and idempotent convergence;
- two-mode three-series retention, patch-release atomicity, protected newest
  releases, whole-storage byte budgeting, and permanent signed tombstones;
- complete exact GC, liveness, generation, byte, object-count, partial-plan,
  and changed-byte rejection;
- source-bound release-cache identity independent of image rollover, complete
  restored-file verification, exact-key-only restore, tamper rejection,
  run/attempt-isolated pull-request transport, and exact main-ref cleanup;
- workflow trigger, acyclic dependency graph, declared job-output use,
  producer-attempt artifact routing, unique YAML mappings, secret boundaries,
  cache/KVM, build/maintenance/qualification/no-op convergence, pre-sign
  decision binding, and publication dependency invariants;
- retry-safe content-derived immutable publication namespaces;
- exact predecessor publication binding at decision, signing, and storage-CAS
  boundaries.

The full kernel and repository workflow adds the expensive evidence that unit
fixtures cannot provide: real v2/v3 ThinLTO compilation, complete build/SIMD
attestation, KVM boot and selftests, cross-flavor package reconciliation,
clean-client installation and DKMS, exact downstream source reconstruction,
production-key signatures, by-hash acquisition, and final signed-repository
verification.

The hosted build cache is intentionally not evidence by itself. A producer
seals it only after all expensive gates pass, and each consumer recomputes its
key from the authenticated lifecycle decision and validates every cached byte.
No build/package/QEMU result is uploaded as a flavor-job artifact. A failed APT
or publication stage preserves the accepted cache for retry; the terminal job
removes it after final remote-state verification, while maintenance and no-op
paths repeat the same exact cleanup idempotently.

The pull-request graph is locally accepted by the repository suite, including
its typed non-publishing decision, forced cache-transport miss, storage/secret
exclusions, and DAG/output invariants. A direct `make
github-apt-repository-qualify` run on 2026-08-19 additionally generated a
disposable key, assembled and signed the binary/source repository, passed the
complete clean client and negative signature cases, and recorded
`publishable=false`. The same flow also passed with `GITHUB_ACTIONS=true`,
covering its CI-only signing guard. This is not a claim that a hosted
pull-request run has completed: hosted acceptance still requires both real
flavor builds and KVM qualifications, the package matrix, and the same
disposable signed clean client to pass on a PR targeting `main`.

[Pull-request run 32221805371](https://github.com/kogeler/dkc-linux/actions/runs/32221805371)
confirmed authenticated source discovery, the fast tier, the typed
`qualification` decision, and release preflight. It did not provide hosted
build acceptance: GitHub suppressed the flavor matrix because its condition
lacked `always()` while the successful decision had deliberately skipped
production-only ancestors. The workflow now combines `always()` with explicit
success requirements for every direct flavor dependency; repository structure
tests enforce that convergence rule.

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
