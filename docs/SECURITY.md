# Security

This document states what DKC defends against, what it does not, and where the
trust boundaries are. It is written to be falsifiable: every claim here should
correspond to something a reader can check in the repository or in a published
artifact.

## Implementation status

The source/index parsers, typed lifecycle and storage boundaries, confined
runner, Debian-only build image, packaging overlay, exact downstream source package, common
binary/source repository, split production signing boundary, signed clean
client, typed signed-state records, conditional publication, lease/fencing,
retention, and exact bounded deletion are implemented. Local real-service
acceptance covered interrupted publication, ambiguous success, lost
preconditions, stale-holder fencing, exact deletion, sanitization, and final
empty-bucket cleanup. The complete hosted build-to-origin lifecycle, its
authenticated no-op retry, and the public delivery data path are accepted.

## Support status, stated plainly

**These kernels will not boot while UEFI Secure Boot enforcement is enabled.**
They are unsigned for Secure Boot purposes. Secure Boot support needs protected
kernel-signing keys, enrolled UEFI or MOK trust, PE verification, in-tree module
signatures, DKMS MOK behaviour, and boot testing with Secure Boot on. None of
that exists here, and none of it is planned for this revision.

**An APT archive signature does not make a kernel Secure-Boot trusted.** These
are unrelated trust domains and are never conflated: the archive key, a kernel
module signing key, and a UEFI signing key are three different things, and the
archive key is never reused as either of the others.

Module signing is deliberately disabled in this initial product. The alternative
is worse: Debian's amd64 rules can generate a fresh signing key per build, which
would make repeated package bytes nondeterministic and would put a freshly
generated private key inside a build container. A fixed public certificate with
a protected deterministic signing stage is possible later, after a key-lifecycle
design exists.

There is a direct kernel dependency behind that choice:
`CONFIG_SECURITY_LOCKDOWN_LSM` selects `CONFIG_MODULE_SIG` whenever loadable
modules are enabled. The initial product therefore also disables the lockdown
LSM and EFI-triggered lockdown. This is an explicit security-capability
deviation from Debian, not an accidental side effect. Re-enabling either
requires the complete protected module and UEFI signing design described above;
claiming lockdown while accepting unsigned modules would give users a false
security guarantee.

## What an attacker would have to defeat

### Getting malicious code into a published kernel

The source comes from Debian and is anchored by hashes at every step:
authenticated `InRelease`, then the `Sources` stanza SHA-256 for the `.dsc`,
then every member hash the `.dsc` declares. A substituted, extra, missing, or
resized member is rejected. The maintainer's OpenPGP signature on the `.dsc` is
checked as defence in depth, never as a replacement for `apt-secure`.

Both release flavors consume the same verified inventory. A moving Sid index
cannot cause one flavor to be built from different bytes than another. The
manual v4 target uses the same locked process but does not enter the release
artifact graph.

GitHub's cache is a transport optimization, not a trust anchor. Production
cache keys bind the source descriptor, downstream revision, tracked build
policy, LTO mode, flavor, and every KVM/selftest/audit policy input. The
immutable build and toolbox image digests remain sealed provenance rather than
cache identity, so an independent image rollover cannot invalidate an accepted
source result. There are no prefix restore keys. The cached path contains no
secret and is readable by pull-request contexts under GitHub's cache model.
Every restore is therefore handled as untrusted input: symbolic/special files,
unexpected paths, changed bytes, identity mismatches, or any non-PASS build,
SIMD, Kbuild, selftest, KVM, or guest result fail closed before package use.
Production and pull-request jobs can reach a save step only after all expensive
gates. Pull requests use a run-and-attempt-qualified transport key in their
merge-ref scope, so they cannot overwrite or suppress work with a `main` entry;
the semantic key inside the handoff is still verified independently. Exact
production cache IDs are removed by the terminal job
after the published signed generation has been read back successfully; the
maintenance and no-op paths perform the same idempotent cleanup, so a later
no-op retry may finish an interrupted deletion.

Workflow artifacts are not authority either. Their attempt-qualified names are
passed from producer job outputs rather than reconstructed by consumers.
Source, decision, state, pool, signing, and repository readers require exact
file inventories and semantic status. Signed state is reauthenticated after
download, and the previous pool is accepted only when every path, byte count,
and SHA-256 matches that signed live-object inventory.

The lifecycle record admits only five exact flag combinations; routing booleans
cannot contradict its selected decision. The secret-bearing signer reloads that
exact record and rejects a signing request that differs in source, revision,
derived package version, build policy, LTO mode, retention mode, whole-storage
limit, generation, or predecessor before importing the private subkey. Final
no-secret verification repeats the decision-to-manifest binding independently,
and the storage CAS compares both the predecessor generation and publication ID
with the live signed pointer.

The final downstream source package is created and reconstructed before any
flavor is compiled. Its `.dsc` names the exact 26-package binary graph and
binds the original and Debian tar members by SHA-256. Every binary package
records `Source: dkc-linux` with the same version. The release gate accepts one
physical source bundle only after both jobs report the same source tree
manifest and build-input identity; the signed archive exposes that bundle
through `Sources` and `deb-src`. The source graph retains v4 so the dormant
flavor remains rebuildable, but no v4 binary enters the signed archive.

Repository licensing is assigned by path in `LICENSES/README.md`. The root MIT
grant covers independent orchestration only. Linux, Debian-derived packaging,
and downstream patches retain their upstream-compatible terms and notices. The
generated source extends `debian/copyright`, preserves the original Linux and
Debian license material, and carries a separate MIT stanza for embedded helper
files. Trademark names describe compatibility and source lineage only; the
project makes no affiliation or endorsement claim.

### Getting a compromised toolchain into a build

The compiler comes from Debian, and the build image asserts that every LLVM
package resolves to a Debian origin. No third-party APT repository is
configured, no third-party key is vendored, and Debian's cryptographic policy is
never relaxed to accept a weaker signature. The base image is pinned by digest,
not by tag.

This is also a user-facing property: Debian's generators put the build compiler
into the runtime dependencies of the headers package, so a compromised or simply
unavailable third-party toolchain would become every DKMS user's problem.

### Reaching a signing key or a storage credential

Secrets live only in protected GitHub Environments, and each secret-bearing job
references exactly one of them:

| Job | Has | Explicitly does not have |
|---|---|---|
| build and test | nothing | every production secret |
| repository assembly and final verification | nothing | every production secret |
| authoritative state read | object-store read-only | signing, write, delete |
| signing | one protected archive signing subkey | primary secret, storage credentials |
| origin publish, finalize, GC | object-store read-write | signing credentials |

No production credential and no private key enters a build container or a build
artifact. Package contents are treated as data and are never executed in a
secret-bearing job. The complete primary secret remains offline; the signing
job proves its imported primary is only a stub and that exactly one secret
subkey is usable. Passphrases are mounted as a mode-0600 file and never passed
through argv where a process list would expose them.

### Publishing without authorization

Production mutation requires the canonical repository, `refs/heads/main`, and
every validation gate at `PASS`. A manual lifecycle additionally requires an
explicit boolean confirmation. Protected Environments use an exact-main
deployment policy but deliberately have no reviewer or wait timer, so the
unattended schedule cannot stall. A stale run is rejected before
signing and again immediately before the secret-bearing storage process.

### Corrupting published state through a race or a crash

Every production write is conditional: immutable objects are created with
`If-None-Match: *`, mutable objects updated with a captured `If-Match` ETag.
There is no unconditional overwrite anywhere in the publication path. Two
concurrent writers produce exactly one success and one HTTP 412, and a 412 stops
the transaction rather than being retried unconditionally.

Crash recovery is defined around two ordered points. Before `InRelease`, the
previous client view and controller state remain authoritative. Between
`InRelease` and the final `state/current.asc` CAS, all client-required bytes are
already present and a repeat of the exact verified artifact is idempotent; a
later lifecycle can also converge conditionally while the previous controller
state still authorizes work. After the state CAS, no client or controller
metadata mutation remains; only exact signed-tombstone deletion and lease
release follow.

### Deleting the wrong object or exceeding the storage limit

Retention has no time delay. A stale client index may therefore name a retired
package that is already absent from origin; edge-cache survival is not treated
as an availability guarantee. The supported path is to refresh metadata before
installing. This is an explicit storage/availability tradeoff, not a cache-TTL
safety claim.

Deletion is by exact key, restricted to immutable prefixes, and derived only
from exact identities in the desired signed tombstone ledger. The complete plan
must fit fail-closed count and byte safety caps before repository commit; it is
never silently split across runs. Each target is re-read and matched by size,
SHA-256, immutable metadata, current generation, and lease ownership. A queued
key is permanently tombstoned and can never become live again. Whole-namespace
size is projected before mutation and listed again after commit and deletion;
the default signed policy rejects a result over decimal 9.5 GB.

## Local execution

Local build, audit, package, and repository workloads run under rootless Podman;
kernel runtime qualification runs in QEMU with KVM. Build and audit containers
prove their confinement before doing any work: not uid 0, no effective
capabilities, and `NoNewPrivs` set. Project sources are streamed in rather than
bind-mounted; accepted multi-gigabyte build results are mounted read-only by the
package auditor. Container root filesystems are read-only where the operation
permits; kernel scratch lives in a run-scoped volume. Namespaces are private,
and there is no network unless a step declares it needs one. Read-only host
probes and GitHub workflow adapters are intentionally not described as
container workloads.

Clean APT/DKMS clients deliberately run as container root with a writable
container filesystem and the capabilities required by package maintainer
scripts. They remain inside rootless Podman's user namespace: no host root or
`sudo` is involved, and only the evidence directory is writable on the host.

Local development, builds, tests, and production operations never use `sudo`
or require root on the host. The completed runner-capacity experiment proved
that deleting preinstalled runner software is unnecessary, so the temporary
cleanup exception and its inventory code have been removed. On a disposable
GitHub-hosted flavor runner only, workflow code uses the runner's documented
privilege mechanism to install QEMU and change the mode of an existing
`/dev/kvm` device. The QEMU package transaction suppresses `needrestart`; the
disposable runner is not upgraded, and its agent must not be restarted during
a job. No udev rule or persistent host configuration is changed. No project
script, make target, package input, or guest validation command receives host
privilege through that exception.

## What is not defended against

- A compromise of Debian's archive, of `deb.debian.org`, or of the Debian
  archive keys. DKC inherits Debian's trust root and does not attempt to
  second-guess it.
- A compromise of GitHub Actions itself, or of a provider account holding the
  bucket or DNS record.
- An attacker who already controls the operator's workstation or the signing
  key backup.
- Malicious content inside a third-party DKMS module a user chooses to install.
- Traffic analysis of who downloads which kernel.

## Reporting

The project has a public delivery endpoint but has not yet published a security
contact. Add a disclosure address and response policy; a placeholder is not a
policy, so none is written here yet.
