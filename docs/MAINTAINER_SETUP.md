# Maintainer setup

Dated GitHub and S3-compatible storage setup, with operator-owned delivery.

## Public CI container images

The repository defines one `container images` workflow for these GitHub
Container registry packages:

```text
ghcr.io/kogeler/dkc-toolbox
ghcr.io/kogeler/dkc-kernel-build
ghcr.io/kogeler/dkc-apt-client
```

No PAT or long-lived registry secret is required. The canonical-main publisher
has job-local `packages: write` permission and logs in with its run-scoped
`GITHUB_TOKEN`. Pull requests have only `contents: read` and cannot authenticate
or publish. The workflow has no package-delete permission.

GitHub creates a new personal package as private unless the account's package
settings say otherwise. After the first publisher has created the three
packages, perform this one-time setup for each package from its GitHub package
settings:

1. Confirm the package is connected to `kogeler/dkc-linux`. The tracked
   `org.opencontainers.image.source` label is intended to create that link; if
   the package page does not show it, connect the repository in the package's
   repository-access settings.
2. Change package visibility to **Public** and complete GitHub's confirmation.
3. Do not grant package administration to Actions and do not add a delete
   token. The workflow needs inherited write access only for publication.

The publishing step logs out immediately after moving all three `latest` tags
and resolves the bundle again anonymously. It therefore fails until all three
packages really are public; an authenticated pull is not accepted as evidence.
The ordinary CI pulls the registry anonymously as well. Its job token has only
`actions: read` so it can wait for an active image publisher before snapshotting
`latest`; no registry credential is exposed to repository code or pull requests.

Verify the one-time setup from an unauthenticated machine:

```sh
podman logout ghcr.io >/dev/null 2>&1 || true
podman pull ghcr.io/kogeler/dkc-toolbox:latest
podman pull ghcr.io/kogeler/dkc-kernel-build:latest
podman pull ghcr.io/kogeler/dkc-apt-client:latest
```

The workflow publishes the bundle only from the canonical repository and
current `main`: on a relevant push, at `09:00 UTC` each Saturday, or by a manual
dispatch whose selected ref is `main`. Selecting another ref skips every job.
The pull-request path runs only when the canonical image input inventory is
affected and performs a build without a push.

## Production lifecycle Environments

Create three GitHub Environments. Each must allow only the `main` branch and
must have no required reviewer, wait timer, or custom protection rule. The lack
of a manual gate is intentional: scheduled source discovery and publication
must finish unattended.

`production-signing` contains only:

```text
APT_GPG_SIGNING_SUBKEY_B64
APT_GPG_PASSPHRASE
```

Generate and install those values by following [KEYS.md](KEYS.md). Never place
the offline primary secret in GitHub.

`production-state-read` contains a genuinely read-only bucket credential and
these exact Environment secret names:

```text
S3_ENDPOINT
S3_REGION
S3_BUCKET
S3_ADDRESSING_STYLE
S3_ACCESS_KEY_ID
S3_SECRET_ACCESS_KEY
S3_SESSION_TOKEN                 optional; omit when unused
```

`production-storage` uses the same names with a separate credential allowed to
read, conditionally create/update, and exactly delete objects in the repository
bucket. It does not need permission to configure the service, buckets, DNS, or
a CDN. Do not reuse a local qualification credential after it has been exposed
or shared; rotate it before provisioning either Environment.

## Rebuilding one Debian source version

`DKC_REVISION` in the top-level `Makefile` is the explicit release revision for
one Debian source version. Increase it before intentionally changing bytes that
the tracked build-policy digest or default LTO mode can affect. The unattended
lifecycle never invents a revision: equal source with a changed policy and an
unchanged revision blocks, a lower revision blocks, and a greater revision
starts the v2/v3 build path. Review the resulting package version before
merging; never lower the value while that Debian source remains published.

## Accepted bootstrap and scheduled operation

The empty origin was initialized and independently verified on 2026-08-18. The
accepted evidence and the corrective authenticated no-op run are recorded in
[VALIDATION.md](VALIDATION.md). Do not repeat empty bootstrap or delete the
signed state pointer to rehearse it.

The six-hour schedule in `ci.yml` now owns unattended source discovery and the
production lifecycle. Ordinary pushes do not start it and cannot authorize a
mutation. The S3 distribution lifecycle itself was already accepted before this
trigger transition and is not awaiting another bootstrap run.

GitHub schedules are best-effort UTC events on the latest default-branch
commit. They may be delayed or dropped under load, and a public repository's
scheduled workflows are disabled after 60 days without repository activity.
Minute 17 deliberately avoids the start-of-hour load peak. Monitor workflow
history and repository `Valid-Until`; if a run is missed, use the exact-main
manual recovery path rather than treating cron as an SLA. See GitHub's
[schedule event documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).

Manual recovery or an intentional bootstrap remains available from `main` through
`workflow_dispatch`. Set `confirm_lifecycle=true`; set
`allow_empty_bootstrap=true` only when the authoritative state read proves the
bucket has no state pointer. Schedules never permit bootstrap.

## Delivery setup

CI stops at the provider-neutral S3 API. The accepted custom-domain and cache
configuration remains operator-owned and is never supplied to the workflow.
The maintained detailed Cloudflare policy and validation recipe is
[CLOUDFLARE_CACHE.md](CLOUDFLARE_CACHE.md); another CDN is allowed when the
operator implements the same path allow-list, bounded positive and negative
TTLs, error handling, and request-rate control.
