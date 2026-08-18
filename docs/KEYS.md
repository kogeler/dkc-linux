# Archive signing keys

The APT archive certificate authenticates repository metadata. It does not sign
the kernel for UEFI Secure Boot and must never be reused as a module, UEFI, SSH,
or Git commit signing key.

The trust boundary has three parts:

- the complete primary secret and revocation certificate stay offline;
- GitHub receives only one passphrase-protected signing-subkey export, whose
  primary secret is a stub;
- the repository tracks only the public bundle and reviewed fingerprints.

The signing workflow creates artifacts but publishes nothing to an external
origin. Storage, retention, and garbage collection are separate concerns.

## Offline provisioning

Use a trusted machine that is disconnected from the network. Its storage must
be encrypted and backed up before the generated material is relied upon. The
machine needs GnuPG 2.4, GNU coreutils, GNU `date`, `make`, and this reviewed
checkout.

Choose a new absolute directory outside the checkout and run:

```sh
make archive-key KEY_WORKSPACE=/secure/offline/dkc-archive-2026
```

The command asks twice for a new passphrase without echoing it. It then:

1. creates a certification-only Ed25519 primary and an Ed25519 signing subkey;
2. gives both an exact GnuPG `4y` lifetime and verifies the resulting
   126,144,000-second validity interval;
3. verifies the automatically generated primary revocation certificate by
   importing it into a disposable public keyring and observing revocation;
4. exports a passphrase-protected complete primary-secret backup and proves it
   can be restored in a disposable offline keyring;
5. exports only the active secret signing subkey, imports it into a disposable
   online keyring, and proves that the primary secret is a stub and exactly one
   secret subkey is usable;
6. records the UTC creation and expiry times under
   `evidence/key-lifecycle.env`;
7. installs exactly these public files into the checkout:

```text
keys/dkc-archive-keyring.gpg
keys/archive-primary.fingerprint
keys/archive-signing-subkeys.fingerprints
```

Review the recorded timestamps and fingerprints on the offline console:

```sh
sed -n '1,20p' /secure/offline/dkc-archive-2026/evidence/key-lifecycle.env
gpg --show-keys --with-subkey-fingerprint \
  /secure/offline/dkc-archive-2026/public/dkc-archive-keyring.gpg
```

The final line of `archive-signing-subkeys.fingerprints` is the active online
signing subkey. The signer selects that exact fingerprint with GnuPG's `!`
suffix, so automatic key selection cannot silently choose another key.

Do not reconnect the offline machine. Copy only `public/` and the two files
under `github/` to encrypted removable media, compare the transfer copies with
the originals byte-for-byte, and carry them to a networked administrator
workstation. Never copy `gnupg/`, `backup/`, or `revocation/` to that
workstation. Copy the three
public files into the same paths under `keys/` in the networked checkout; the
provisioning checkout already shows the exact intended paths.

## Offline backup and revocation

The workspace contains sensitive material:

```text
gnupg/                                      complete working keyring
backup/dkc-archive-primary-secret.asc      protected complete secret backup
revocation/<PRIMARY>.rev                   primary revocation certificate
github/APT_GPG_SIGNING_SUBKEY_B64          online secret-subkey handoff
github/APT_GPG_PASSPHRASE                  plaintext handoff passphrase
```

Keep at least two offline copies of the complete secret backup and revocation
certificate on separately stored encrypted media. Keep the passphrase in a
separate password manager or sealed recovery record. The provisioning command
already performs one scratch restore, but an operator must also restore a copy
from the actual backup medium before destroying the original working keyring.
Compare the restored fingerprints to the three tracked public files.

The revocation certificate is emergency material. Importing it revokes the
whole certificate; do so only after compromise or irreversible key loss, then
publish the updated public bundle through a reviewed incident procedure. A
normal planned rotation does not use the revocation certificate.

## GitHub Environment

Create an Environment named `production-signing`. GitHub makes Environment
secrets available only to jobs that reference that Environment and only after
its protection rules pass. GitHub documents Environment configuration at
<https://docs.github.com/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments>.

In the web interface:

1. open **Settings → Environments → New environment**;
2. name it `production-signing`;
3. choose **Selected branches and tags** and add one branch rule named exactly
   `main`;
4. do not enable **Required reviewers**, **Wait timer**, or any custom
   deployment protection rule;
5. add Environment secrets named `APT_GPG_SIGNING_SUBKEY_B64` and
   `APT_GPG_PASSPHRASE` from the two files under `workspace/github/`.

The absence of manual approval is a required automation property, not an
operator preference. Scheduled discovery runs must be able to build,
test, and sign a newly detected kernel without a person releasing the signing
job. GitHub applies wait timers and required reviewers even when a job uses
`deployment: false`, so the exact-main branch policy must be the Environment's
only protection rule. That interaction is documented in
<https://docs.github.com/actions/how-tos/deploy/configure-and-manage-deployments/control-deployments>.

An administrator can create the same exact-main Environment with the GitHub
CLI and REST API. Run the branch-policy creation command only once; GitHub
returns a redirect when an identical policy already exists.

```sh
repo=kogeler/dkc-linux

gh api --method PUT \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  "repos/${repo}/environments/production-signing" \
  --input - <<'JSON'
{
  "wait_timer": 0,
  "prevent_self_review": false,
  "reviewers": [],
  "deployment_branch_policy": {
    "protected_branches": false,
    "custom_branch_policies": true
  }
}
JSON

gh api --method POST \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  "repos/${repo}/environments/production-signing/deployment-branch-policies" \
  -f name=main -f type=branch

gh secret set APT_GPG_SIGNING_SUBKEY_B64 \
  --repo "$repo" --env production-signing \
  < /secure-transfer/dkc-archive/github/APT_GPG_SIGNING_SUBKEY_B64

gh secret set APT_GPG_PASSPHRASE \
  --repo "$repo" --env production-signing \
  < /secure-transfer/dkc-archive/github/APT_GPG_PASSPHRASE
```

`gh secret set` encrypts the value locally before sending it. GitHub limits
each Actions secret to 48 KiB; provisioning and the signing wrapper both enforce
that limit. The CLI reference is
<https://cli.github.com/manual/gh_secret_set>, and GitHub's current secret
limits are documented at
<https://docs.github.com/actions/reference/security/secrets>.

Verify names, the absence of a manual gate, and the exact single branch rule
without retrieving secret values:

```sh
gh secret list --repo kogeler/dkc-linux --env production-signing

environment_check="$(gh api \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/kogeler/dkc-linux/environments/production-signing \
  --jq '
    select(.deployment_branch_policy.protected_branches == false)
    | select(.deployment_branch_policy.custom_branch_policies == true)
    | select([.protection_rules[]
        | select(.type == "required_reviewers" and (.reviewers | length) > 0)]
        | length == 0)
    | select([.protection_rules[]
        | select(.type == "wait_timer" and .wait_timer > 0)]
        | length == 0)
    | select([.protection_rules[]
        | select(.type != "branch_policy"
            and .type != "required_reviewers"
            and .type != "wait_timer")]
        | length == 0)
    | .name
  ')"
test "$environment_check" = production-signing

policy_count="$(gh api \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/kogeler/dkc-linux/environments/production-signing/deployment-branch-policies \
  --jq '.branch_policies | length')"
main_policy_count="$(gh api \
  -H 'X-GitHub-Api-Version: 2026-03-10' \
  repos/kogeler/dkc-linux/environments/production-signing/deployment-branch-policies \
  --jq '[.branch_policies[] | select(.name == "main" and .type == "branch")] | length')"
test "$policy_count" = 1
test "$main_policy_count" = 1
```

After both secrets are present, remove the networked and transfer-media copies
of the two handoff files using the secure-erasure procedure appropriate to
their encrypted storage. Also remove the offline `workspace/github/` handoff
copies after confirming the Environment secrets exist; they are not backups.
Never copy them into the checkout, an issue, a workflow log, or a shell command
line. Keep the protected offline primary backup and revocation certificate.

Commit only the public material, review the diff, and push `main`:

```sh
git add \
  keys/dkc-archive-keyring.gpg \
  keys/archive-primary.fingerprint \
  keys/archive-signing-subkeys.fingerprints
git diff --cached --stat
git commit -m 'Provision the APT archive certificate'
git push origin main
```

The push publishes only the reviewed public material; ordinary pushes do not
start the production lifecycle. After it becomes canonical `main`, either wait
for the six-hour schedule or start one exact-main manual run with
`confirm_lifecycle=true`. Keep the run URL for validation before treating
provisioning as complete. The complete graph performs source discovery, the
v2/v3 build and QEMU gates when required, package reconciliation, repository
signing, final-client validation, and conditional storage publication. The
retained v4 implementation is outside the automatic release as documented in
[`BUILD.md`](BUILD.md#release-flavor-policy).

## Signing flow

A trusted build decision from the unattended schedule or an accepted exact-main
manual run follows this sequence:

1. two independent jobs restore exact accepted v2/v3 cache entries or, on a
   miss, build and exercise those flavors in KVM before sealing the entries;
2. a no-secret job reconciles the package matrix, builds one unsigned
   19-binary/two-source repository, and emits a strict request containing every
   accepted path, size, and SHA-256;
3. a no-secret gate resolves canonical `main` and rejects a stale workflow;
4. the only job attached to `production-signing` repeats that gate, requires
   the complete signing request to match the typed lifecycle decision, then
   imports the secret-subkey export in a temporary `GNUPGHOME`, proves the
   primary secret is unavailable, rejects stale metadata or a certificate
   inside the configured safety horizon, and emits only an eleven-file
   signature/state overlay;
5. a final no-secret job rejects extra, missing, replaced, or unsigned files,
   merges the overlay, verifies the exact active subkey, and runs the complete
   clean-client APT, release-kernel installation, `deb-src`, by-hash,
   source-rebuild, keyring-install, and corrupt/missing-signature tests;
6. only after that boundary, the storage job conditionally commits the exact
   artifact, a separate read-only job verifies the signed generation, and the
   terminal job removes both exact release caches.

A metadata-maintenance decision reuses the authenticated live pool and starts
at repository assembly; a typed no-op never reaches the signing Environment.

The final repository artifact is retained for seven days. The signing job has
no storage, release, branch-write, tag-write, or package-publication credential;
the storage jobs have no signing credential.

The final no-secret client job is the mandatory publication boundary.
`publish-repository` declares `verify-repository` in `needs`, consumes only its
complete verified repository artifact, and never depends directly on the
signing job as a substitute for client installation testing.

For a local mechanics test, use disposable keys explicitly:

```sh
DKC_APT_EPHEMERAL_SIGNING=1 make apt-repository
```

That switch is rejected whenever `GITHUB_ACTIONS` is set. Without it, the
target requires the tracked public files and the two production secret
environment variables.

## Replacement and rotation

Monitor both UTC expiry timestamps recorded during provisioning. Begin planned
overlap rotation no later than 180 days before the active subkey expires. The
signer also refuses a new signature unless the active subkey remains valid
beyond the 14-day `Valid-Until`, a one-day clock-skew allowance, and a 30-day
signing safety interval.

For a signing-subkey replacement under the same still-valid primary:

1. create the new signing subkey offline;
2. retain the old public subkey, append the new fingerprint as the final line,
   and export the updated public bundle;
3. export only the new secret subkey and repeat the online-stub verification;
4. replace both GitHub Environment secrets;
5. commit the three updated public files and require a complete CI pass;
6. keep the old public subkey through the overlap period so existing metadata
   remains verifiable.

Because the initial primary and subkey expire together, also begin successor
primary-certificate design at the 180-day alert. The current bootstrap
validator intentionally accepts one primary only; support for an overlapping
successor primary must be implemented and reviewed before that rollover rather
than improvised during an expiry incident.
