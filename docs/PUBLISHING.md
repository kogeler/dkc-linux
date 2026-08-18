# Publishing

The main workflow discovers, builds, signs, verifies, and publishes one common
APT repository without an operator handoff between stages. Pull requests never
receive production secrets and cannot enter this graph.

## Lifecycle decisions

Every trusted run independently obtains two inputs:

- the newest `src:linux` descriptor from Debian's authenticated Sid index;
- the authoritative signed state read directly through the S3 API with a
  read-only credential.

The no-secret decision step accepts only exact typed handoffs. Source-derived
environment fields are reconstructed and compared; signed state is reverified
with the tracked OpenPGP key, then its pointer, manifest hash, generation, and
file boundary are checked again after artifact transport. The step returns
exactly one result:

- `build`: the repository is authoritatively empty and bootstrap was explicitly
  allowed, Debian has a newer version, or the same source has a strictly higher
  configured downstream revision;
- `maintenance`: the newest version is already present but signed metadata is
  inside its refresh horizon, the configured retention policy differs from the
  signed policy, or the measured storage namespace exceeds the signed limit;
- `no_op`: source, metadata, retention policy, and measured storage are current;
- `blocked`: state was not authoritatively readable, bootstrap was not allowed,
  source discovery moved behind signed state, an equal Debian version appeared
  with a different descriptor hash, the configured downstream revision moved
  backwards, or build policy/LTO changed without a revision increase.

For one Debian source version, `DKC_REVISION` is the explicit authorization to
publish different build bytes. The decision hashes the same tracked build-policy
inputs used by build identity and records the LTO mode. Equal revision plus a
different policy digest or LTO mode fails closed; it never silently overwrites a
package version. Before publication, the clean-client-verified signed manifest
must match the decision's source, revision, build-policy, LTO, retention, and
predecessor fields.

A build compiles and exercises v2 and v3 in parallel, reconciles their package
graphs, and creates a new generation. Before compiling, each flavor attempts a
main-branch release-cache restore. The current key binds the authenticated
Debian descriptor, downstream revision/build policy, validation policy, flavor,
and LTO mode; container image digests are recorded separately as build
provenance. Only an exact key can be restored. Build + attestation + selftests +
KVM qualification must all pass before a cache is populated, and every consumer
verifies its full inventory and semantic PASS records. A downstream failure
therefore permits a cheap retry, while the terminal job deletes both exact keys
after successful build or maintenance publication and repeats that exact
cleanup on no-op. A failed-jobs-only retry receives producer-selected artifact
names rather than guessing them from its own attempt number; a no-op retry can
therefore finish cache cleanup without rebuilding or republishing. Maintenance
downloads the previous signed live pool and rebuilds indexes, `Release`,
signatures, and state without compiling a kernel. A no-op reaches no signing or
mutation job.

## Secret separation

The unsigned repository and its exact signing request are produced without a
private key. Before importing the protected archive subkey, the signing job
requires the request's source identity, downstream revision, derived package
version, build policy, LTO mode, retention mode and byte limit, generation, and
predecessor to match the exact typed lifecycle handoff. It receives no storage
credential and executes no package content. The following job has no secrets,
merges only the bounded signature overlay, verifies all signatures and hashes,
and proves installation, upgrade, both release kernels, headers/DKMS, `deb-src`,
by-hash, and negative signature cases in a clean client.

Only that verified repository artifact can enter the storage job. The storage
job receives no signing secret. The authoritative state read and final state
verification use a separate read-only credential.

## Conditional commit

The storage job repeats the canonical-main check with connection variables and
the run token removed from the check process. It then acquires
`state/locks/production.json` with a conditional request. The lease owner binds
the repository, workflow run, attempt, operation, and a random nonce. Every
mutation checkpoint direct-reads the exact lease ETag and owner, requires a
safe remaining time window, and renews with `If-Match`.

Immediately before planning writes, the desired signed manifest's predecessor
generation and publication ID must equal the freshly authenticated
`state/current.asc`. Matching only the generation number is insufficient.

The publication sequence is:

1. validate the clean-client result, signatures, signed manifest, state pointer,
   checksums, object sizes, media types, and cache classes;
2. plan every mutable key against its currently observed ETag;
3. conditionally create the signed transaction record;
4. conditionally create or exact-byte-reuse pool, by-hash, and immutable state
   objects;
5. update canonical indexes, `Release`, detached release signature, and public
   key paths with their captured preconditions;
6. update `dists/trixie/InRelease` with its captured precondition; this is the
   APT-client commit point;
7. authenticated-read and verify every committed repository object;
8. update signed root manifest/checksum conveniences, signatures first;
9. update `state/current.asc` last with its captured precondition; this is the
   controller commit point;
10. authenticated-read and verify the complete final mutable view;
11. apply the complete exact plan derived from signed tombstones within
    fail-closed count and byte safety caps while the same lease remains held;
12. release the exact lease revision with `If-Match`.

Immutable publication and transaction namespaces are derived from the complete
strict request plus both signed Release forms. A same-day retry with a new
timestamp or signature therefore cannot collide with immutable objects left by
an interrupted attempt.

Immutable writes use `If-None-Match: *`; mutable writes use either that exact
create precondition or one captured `If-Match` ETag. There is no unconditional
write entry point. A lost 2xx response is reconciled only by an authenticated
read of the exact intended bytes and HTTP metadata. A 412 is never reconciled
or retried as success.

The state pointer is deliberately the final publication mutation. If a process
stops before it, the previous signed state remains controller authority and a
later run can safely converge by conditionally replacing the incomplete
client-visible generation. Once the state CAS succeeds, all client metadata
and conveniences are already present; only verification and bounded deletion
remain. Repeating the exact plan is idempotent.

If release fails during another failure, the original publication error remains
the reported cause. The lease is left for safe takeover only after expiry,
configured grace, and proof that the recorded workflow attempt is terminal;
expiry by itself never permits takeover.

## Triggers and bootstrap

The production lifecycle runs on the six-hour schedule or an exact-main manual
dispatch. Ordinary pushes do not start or authorize it. Manual dispatch
requires `confirm_lifecycle=true`; only an explicitly requested manual bootstrap
may set `allow_empty_bootstrap=true`.

The production origin already has an accepted generation-zero state. Every
accepted canonical-main trigger runs the lifecycle without a separate enable
switch, and a schedule can never authorize empty bootstrap.
