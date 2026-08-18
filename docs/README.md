# Documentation

DKC documentation distinguishes code that exists from evidence that has
actually been collected. The implementation, destructive storage cases, first
hosted production lifecycle, generation-zero publication, authenticated no-op
retry, and public APT delivery have passed.

## Users

- [USER_INSTALL.md](USER_INSTALL.md) — trust bootstrap, flavor selection,
  installation, upgrades, rollback, removal, and source retrieval against the
  public endpoint.
- [SECURITY.md](SECURITY.md) — trust boundaries, supported threat model, Secure
  Boot limitation, and secret separation.

## Contributors

- [BUILD.md](BUILD.md) — local images, source policy, flavor builds, ThinLTO,
  attestation, package reconciliation, and GitHub release caches.
- [KERNEL_TESTING.md](KERNEL_TESTING.md) — KVM guest qualification and the
  maintained kernel selftest profile.
- [CI_CAPACITY.md](CI_CAPACITY.md) — accepted standard-runner measurements and
  runner policy.
- [VALIDATION.md](VALIDATION.md) — repeatable checks, completed local storage
  acceptance, and hosted production evidence.

## Maintainers

- [MAINTAINER_SETUP.md](MAINTAINER_SETUP.md) — registry, protected GitHub
  Environments, accepted bootstrap state, and scheduled operation.
- [KEYS.md](KEYS.md) — four-year archive key provisioning, offline primary-key
  custody, and signing-subkey rotation.
- [PUBLISHING.md](PUBLISHING.md) — lifecycle decisions, signing separation, and
  the conditional publication transaction.
- [STORAGE.md](STORAGE.md) — provider-neutral S3 contract, credential
  confinement, redaction, and disposable qualification.
- [RETENTION.md](RETENTION.md) — retained release series, whole-storage size
  policy, tombstones, and immediate bounded exact deletion.
- [CLOUDFLARE_CACHE.md](CLOUDFLARE_CACHE.md) — accepted operator-owned delivery
  configuration and its repeatable validation; CI does not call a CDN API.

## Design and compatibility

- [ARCHITECTURE.md](ARCHITECTURE.md) — trust domains, data flow, commit points,
  concurrency, and recovery.
- [debian-overlay/README.md](../debian-overlay/README.md) and
  [debian-overlay/COMPATIBILITY.md](../debian-overlay/COMPATIBILITY.md) — tracked
  Debian packaging changes and the compatibility gap they close.
- [config/flavors/README.md](../config/flavors/README.md) — flavor and SIMD
  policy.
- [LICENSES/README.md](../LICENSES/README.md) — per-path licensing and inherited
  Linux/Debian terms.
- [schemas/README.md](../schemas/README.md) — typed machine-readable handoffs.
- [.github/workflows/README.md](../.github/workflows/README.md) — declarative CI
  structure and workflow-specific make adapters.

`make help` is the canonical target inventory. A check may be described as
`PASS` only after that exact execution has completed successfully; remaining
maintenance work is tracked in [TODO.md](../TODO.md).
