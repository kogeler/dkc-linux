# Schemas

These versioned JSON Schemas define the records that cross build, signing, and
storage trust boundaries:

- `source-inventory` — authenticated Debian source members and hashes;
- `authoritative-state-read` — exact result of one signed state read;
- `pool-export` — size and object count for a manifest-bound live pool;
- `discovery-decision` — one exact build, maintenance, no-op, qualification, or
  blocked result;
- `provenance` — one accepted build identity and its inputs;
- `repository-signing-request` — exact unsigned repository bytes authorized for
  the confined signing boundary;
- `publication-manifest` — immutable signed generation, live-object set, and
  retention state;
- `state-pointer` — small signed pointer to the authoritative manifest;
- `transaction` — immutable intended publication transaction;
- `production-lease` — conditionally updated mutation lease and fencing owner;
- `gc-plan` — signed-tombstone-derived, generation-bound, bounded
  exact-deletion plan.

Records reject unknown fields. Canonical serialization, schema validation, and
cross-record identity checks are covered by repeatable tests; a schema-valid
record alone is never treated as authenticated. Decision routing flags are
fixed by the selected decision rather than accepted as independent booleans.
