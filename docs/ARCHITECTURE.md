# Architecture

DKC is a make-backed pipeline with four trust domains. GitHub Actions supplies
ordering and isolated runners; repository scripts implement the actual build,
verification, signing, and publication protocol.

## Data flow

1. A no-secret network job authenticates Debian's Sid metadata and emits one
   hash-bound source inventory, including the exact archive member names used
   by the descriptor rather than locally reconstructed version templates.
2. On a production trigger, a separate read-only storage job verifies the
   signed authoritative state. Pull requests receive no storage credential and
   do not execute this step.
3. A no-secret decision selects build, metadata maintenance, no-op, blocked,
   or the pull-request-only qualification state. Production decisions bind the
   configured downstream revision, tracked build-policy digest, LTO mode,
   retention mode, and whole-storage byte limit to the signed prior manifest.
   Qualification deliberately has no prior-state or publication authority and
   always requires fresh v2/v3 builds.
4. Build jobs derive exact content keys, restore or compile v2 and v3
   independently, attest SIMD/LTO and packaging, boot newly built results with
   KVM, and run exact-source kernel selftests. Production can restore an exact
   verified main-branch cache entry. Pull requests use run-and-attempt-isolated
   transport keys that force both builds and VMs to execute.
5. A no-secret convergence job restores and verifies both exact cache entries,
   reconciles the 18 unique packages, validates
   clean package clients, merges the authenticated prior live pool when one
   exists, and emits a strict unsigned repository/signing request.
6. The signing Environment revalidates the complete unsigned handoff and
   requires its source, downstream version, build policy, LTO mode, retention
   mode and byte limit, generation, exact predecessor publication, and derived
   package version to match the typed lifecycle decision before importing the
   archive subkey. It then returns a bounded signature/state overlay.
7. A no-secret client reconstructs the complete repository, requires its signed
   manifest to match the lifecycle decision, and verifies install, update,
   source, by-hash, and negative trust paths.
8. The storage Environment conditionally commits only that artifact, performs
   complete bounded GC under the same lease, and releases it.
9. A separate read-only storage job verifies the intended final signed
   generation. The terminal job then removes the two exact release caches with
   a narrowly scoped Actions permission; a no-op retry can repeat only this
   cleanup after an earlier cache-API failure.

Pull requests run source discovery, fast and release-input checks, both flavor
builds and KVM qualifications, package convergence, and a complete repository
test signed with a disposable key. They cannot read authoritative state, enter
the production signing Environment, publish, or run production cache cleanup.
No build artifact, package script, or source file executes inside a
secret-bearing job. Both production publication branches require the fast tier
to pass before signing or external mutation.

## Commit points

`dists/trixie/InRelease` is the APT-client commit point. All pool objects,
by-hash indexes, canonical indexes, `Release`, and its detached signature exist
and have been origin-verified before this conditional write. Retention does not
promise origin availability for package paths named only by stale client
metadata; clients should refresh metadata before installing.

`state/current.asc` is the controller commit point and the final publication
mutation. It is a clearsigned pointer to one immutable signed manifest and
commits only after client metadata plus root conveniences are complete. Build,
maintenance, recovery, and GC decisions trust only an authenticated S3 read of
this pointer and manifest; public delivery is a replayable hint, not authority.

## Concurrency and failure

All trusted lifecycle events share one non-cancelling GitHub concurrency group.
The storage critical section adds a conditional lease whose owner includes the
exact workflow attempt and a random nonce. ETag/owner checks and renewal fence
every mutation. Stale takeover requires lease expiry, additional grace, and
proof that the previous workflow attempt is terminal.

Every write has a captured precondition. Immutable collisions accept only exact
bytes and HTTP metadata. Ambiguous transport failures are reconciled by direct
read; genuine precondition loss stops. A failure before the state commit leaves
the previous controller generation authoritative and can be converged by a
later conditional run. A failure after it cannot leave an incomplete published
view because state is written last.

Every cross-job artifact name is exported by its producer as a job output.
Consumers never reconstruct a name from their own attempt number, so both a
full re-run and a failed-jobs-only re-run select the artifact from the attempt
that actually produced it. Downloaded source and lifecycle directories must
satisfy exact checksum and typed-field contracts. Downloaded state is
additionally reauthenticated against the tracked OpenPGP key, and a live-pool
export is checked byte-for-byte against that signed manifest before reuse.
The decision carries both the signed predecessor generation and publication ID;
the signer and the final storage CAS require that pair to remain unchanged.

Accepted build caches are retry state, not authority. Their keys bind the
authenticated source, downstream revision/build policy, validation policy,
flavor, and LTO mode; container image digests are independent provenance. The
validation policy covers attestation, selftests, and QEMU inputs, so changing an
acceptance criterion cannot reuse an older PASS. Every restore is checked
against a complete file inventory and the typed lifecycle decision before
package processing. Only an exact key may be restored; there is no prefix
fallback or compatibility migration. A downstream failure keeps the cache,
while the terminal job deletes the exact keys after a successful build or
maintenance publication, or during a no-op cleanup retry.
Cache eviction or absence merely causes the full build and VM qualification to
run again.

## Storage and delivery

The implementation uses only a small provider-neutral S3-compatible API.
Endpoint, region, bucket, addressing style, and credentials are Environment
secrets and are confined to mode-0600 mounted files. CI has no CDN URL,
credential, purge call, probe, or control-plane action. Public delivery, its
strict path allow-list, and bounded cache TTLs are one-time operator
configuration described separately from the publication protocol.
