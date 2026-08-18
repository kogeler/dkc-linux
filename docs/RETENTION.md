# Retention and garbage collection

Retention uses parsed Debian versions, not filenames or lexical ordering. One
upstream patch release is indivisible: its v2/v3 binaries, Debian revisions,
downstream revisions, and source-package members enter or leave together.

Both modes keep only the newest three upstream `X.Y` series:

- `series` keeps every patch release in those three series. When a fourth
  series appears, every release in the oldest series is retired.
- `series-size` applies the same rule, then enforces a whole-object-storage
  byte limit. It removes complete patch releases globally from oldest to
  newest, but always protects the newest patch release in each retained
  series. If those protected releases cannot fit, assembly fails rather than
  remove an entire series.

`series-size` is the default. Its tracked production limit is
`9,500,000,000` bytes (decimal 9.5 GB). Select the unbounded mode explicitly
with `APT_RETENTION_MODE=series`; that mode rejects a byte-limit argument.
The lifecycle decision signs these policy inputs into its handoff. A mismatch
with current signed state, or a measured namespace already over the limit,
selects metadata maintenance rather than `no_op`; kernel packages are reused.

## Whole-storage accounting

The size policy covers the complete configured object-storage namespace, not
only `.deb` files. An authenticated state read lists every object and records
the exact count and byte total, including objects left by an interrupted
pre-commit bootstrap. Assembly subtracts the prior signed live pool, adds the
candidate pool, and reserves 64 MiB for replacement metadata, signed state,
transaction records, and lease variation.

Immediately before mutation, the publisher lists the namespace again and
projects the exact result of every intended write and deletion. A capped
publication exceeding its signed limit is rejected before the repository
commit. After commit, deletion, and lease release, another complete listing
must satisfy the same limit. The final count and byte total are retained as
sanitized publication evidence.

## Logical retirement

Assembly starts from the previous signed manifest and an authenticated export
of exactly its live pool. It verifies every prior byte, size, hash, and cache
class, applies retention, then regenerates complete multi-version `Packages`,
`Sources`, compressed indexes, by-hash objects, `Release`, and the signing
request.

Every previously live immutable object absent from the new tree becomes a
signed tombstone containing its exact key, SHA-256, size, and reason. Existing
tombstones remain in the signed ledger. A tombstoned key can never become live
again; restoration requires a new package revision and therefore a new
immutable key. `live_objects` is the only liveness set. Audit references do
not keep retired payloads live.

There is no age-based retention and no deletion delay. Clients should update
repository metadata before requesting packages; an old cached index can name
an object already removed from the origin. Edge caches may still serve such an
object, but their presence is not an availability guarantee.

## Physical deletion

Before committing new state, the publisher derives one complete deletion plan
from the desired signed tombstone ledger. Every existing target must:

- be an exact key under an allowed immutable path;
- be absent from current `live_objects`;
- match the signed size, SHA-256, and immutable cache class;
- fit together with every other target under the configured safety caps.

The defaults allow 10,000 objects and 10,000,000,000 bytes. These are
fail-closed safety bounds, not incremental retention: if the complete plan
does not fit, publication stops before commit. Missing targets are already
complete. After the new signed state commits, all planned objects are deleted
immediately while the same lease remains held. Generation and lease ownership
are rechecked around every exact deletion. Prefix, wildcard, lifecycle-rule,
and unconditional bucket cleanup are never used.

Disposable qualification cleanup is unrelated to production retention. It is
confined to its validated test prefix and proves a zero-object final listing.
