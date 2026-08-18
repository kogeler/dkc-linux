# Object storage integration

The repository publisher uses a provider-neutral S3-compatible data path. It
does not select a vendor by hostname or by a provider-specific SDK. The
implemented boundary is deliberately small:

- Signature Version 4 over HTTPS;
- path-style or virtual-hosted addressing;
- literal single-request `PutObject` with exactly one of `If-None-Match: *` or
  an opaque `If-Match` ETag;
- authenticated `GetObject`, exact `DeleteObject`, and paginated
  `ListObjectsV2`;
- exact bytes, size, `Content-Type`, and `Cache-Control` verification after
  upload.

A compatible service must preserve those conditional-write semantics. An S3
label alone is not sufficient; the controlled local qualification must pass
before the backend can be accepted.

## Secret boundary

Connection details are not tracked as configuration and are not ordinary
workflow variables. Production jobs receive these values only from their
dedicated protected storage Environments:

```text
S3_ENDPOINT
S3_REGION
S3_BUCKET
S3_ADDRESSING_STYLE
S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY
S3_SESSION_TOKEN                 optional
```

`S3_ADDRESSING_STYLE` is exactly `path` or `virtual`.

Restrict every machine-run Environment's deployment branch policy to `main`,
and add no required reviewer or wait timer. The lifecycle repeats current-main
validation immediately before the secret-bearing publication step. Use a
disposable bucket for local qualification. If credentials can be scoped only to a whole
bucket rather than the nonce prefix, treat that limitation explicitly: the
repository code is the prefix guard, not a provider-enforced claim of narrower
permission.

In GitHub, the host wrapper converts Environment secrets to a mode-0600
temporary JSON file, removes them from its environment, and mounts the file
read-only into the confined rootless container. No secret is passed through container arguments,
container environment, an image layer, an artifact, or an evidence record.
Endpoint, region, bucket, addressing mode, credentials, and the run-scoped API
token are redacted from bounded command output and tracebacks. The sanitizer
covers raw, URL/form-encoded, JSON-escaped, Base64, URL-safe Base64, and hex
forms, strips control characters, and neutralizes workflow-command syntax from
remote error bodies. Normal output contains only operation status, object
counts, hashes, and local evidence paths. Automatic log masking remains useful
defense in depth, but the implementation does not depend on it.

For a local real-service run, prefer an operator-owned file outside the
repository instead of environment variables. The file must be a regular
non-symlink owned by the invoking user, grant no group or other permissions,
and contain exactly:

```json
{
  "s3_access_key_id": "REPLACE",
  "s3_addressing_style": "path",
  "s3_bucket": "REPLACE",
  "s3_endpoint": "https://storage-endpoint.example",
  "s3_region": "REPLACE",
  "s3_secret_access_key": "REPLACE"
}
```

Add `s3_session_token` only when the service issued one. Create the file with a
restrictive umask and verify its mode before use:

```sh
umask 077
mkdir -p /media/secrets/apt
${EDITOR:?set EDITOR} /media/secrets/apt/storage-integration.json
chmod 600 /media/secrets/apt/storage-integration.json
stat -c '%a %U %n' /media/secrets/apt/storage-integration.json
```

Do not paste the access key or secret into chat, a Make command line, shell
history, repository configuration, or an environment file.

The workflow has no public-delivery URL, CDN credential, purge token, or CDN
API endpoint. It neither configures nor probes a CDN. The accepted delivery
configuration is operator-owned; its maintained Cloudflare policy and
validation recipe is in [CLOUDFLARE_CACHE.md](CLOUDFLARE_CACHE.md). Another CDN
is permitted when the operator supplies an equivalent public-path allow-list,
positive and negative cache policy, and request-rate control.

## Production data path

The main workflow is the only hosted publication path. It uses three separate
boundaries:

1. `make storage-state-read` uses the read-only Environment to fetch
   `state/current.asc`, verify its clearsignature with the tracked keyring,
   fetch the named immutable manifest, and verify its hash, signature, schema,
   generation, object metadata, and opaque ETags. Absence of the state pointer
   is recorded as an authoritative empty result; a failed read is never treated
   as empty.
2. When prior state exists, `make storage-export-pool` downloads only pool
   objects in that signed manifest's explicit `live_objects` set. Every byte,
   size, media type, and immutable cache policy must match before the next
   repository can be assembled.
3. After signing and no-secret clean-client verification,
   `make storage-publish` repeats the canonical-main check with every storage
   variable removed, confines connection data to a mode-0600 mounted file,
   acquires the conditional production lease, verifies the expected generation,
   projects the exact whole-namespace byte result, executes the twelve-phase
   publication, applies the complete signed tombstone set within fail-closed
   safety caps, verifies the final generation and storage size, and releases
   the lease.

All immutable keys use conditional create and exact-byte reuse. Each mutable
key uses the ETag captured during planning. `dists/trixie/InRelease` is the APT
client commit point; `state/current.asc` follows as controller authority. A
lost response is accepted only after an authenticated read proves the exact
intended bytes and metadata. HTTP 412 is always a lost precondition, never a
retry signal. See [PUBLISHING.md](PUBLISHING.md) and
[RETENTION.md](RETENTION.md).

The toolbox image is resolved and verified before a storage step receives
secrets. The secret-bearing `make` invocation explicitly disables image
prerequisites, so connection values cannot enter registry or image-build
processes.

## Disposable qualification

The local qualification target is:

```sh
make storage-disposable \
  STORAGE_REPOSITORY_RESULT=/absolute/path/to/a/verified/repository/result
```

The target consumes the complete result produced by `make apt-repository` only
after its evidence checksums and all clean-client gates verify. It generates a
cryptographic nonce and permits keys only below:

```text
_dkc-test/storage/<repository-hash>/<run-id>-<nonce>/
```

It conditionally uploads the complete repository, authenticates and verifies
every object directly through S3, forces `ListObjectsV2` pagination with a
ten-object page size, and executes an exactly-one-winner ETag race. It then
deletes each fixture key exactly. The final authenticated prefix listing must
contain zero objects. Missing S3 connection fields block before the repository
is read or the first remote request is made.

Run the real local qualification with:

```sh
make storage-disposable \
  STORAGE_CONNECTION_FILE=/media/secrets/apt/storage-integration.json \
  STORAGE_REPOSITORY_RESULT=/absolute/path/to/out/apt-repository/RUN
```

The connection-file path is not secret, but its contents never enter argv,
container environment, logs, or evidence. The result directory under
`out/storage-disposable/` is durable. Before the first S3 request it receives a
mode-0600 `cleanup.json` with the exact nonce prefix and a separate redacted
inventory containing only key and prefix hashes. The cleanup journal is
excluded from CI artifacts and evidence checksums. A normal pass cleans the
prefix automatically.

If the host, shell, or container is interrupted after mutation starts, recover
with the exact retained result rather than starting another qualification:

```sh
make storage-disposable-cleanup \
  STORAGE_CONNECTION_FILE=/media/secrets/apt/storage-integration.json \
  STORAGE_DISPOSABLE_RESULT=/absolute/path/to/out/storage-disposable/RUN
```

Recovery accepts only the strictly validated `_dkc-test/storage/` prefix stored
in that result, deletes exact listed keys, and requires a final empty prefix.
Its separate evidence is stored under `out/storage-cleanup/`. If interruption
happened before `cleanup.json` was written, no S3 request had begun.

This cleanup is test hygiene. It is not the package-retention or garbage-
collection implementation and it grants no authority to touch a production
prefix.

Disposable qualification and destructive fault injection are local-only
implementation acceptance procedures. They are not GitHub Actions workflows.
The main CI lifecycle contains the sole hosted storage path after its ordinary
build, VM, repository-signing, and clean-client verification gates.
