# Workflows

Workflow YAML is deliberately declarative. An executable step invokes exactly
one `make` target and may pass only explicit make variables; shell control flow,
parsing, package installation, and direct script calls belong in tested project
code. Targets that exist only as adapters for GitHub command files or hosted
runner setup start with `github-` and live in `mk/github.mk`. Provider-neutral
release gates remain ordinary targets so the same handoffs can be checked
locally.

Secret-bearing jobs resolve and verify their registry image in a separate
credential-free step. Their `github-*` execution target then reuses the
corresponding prepared operation without performing image resolution while
secrets are present. Workflow command-file writes are idempotent: repeating the
same typed assignment is a no-op and a conflicting assignment fails closed.
Artifact producers export their actual attempt-qualified name as a job output.
Consumers use that output instead of reconstructing a name from their own
attempt, so failed-jobs-only re-runs retain valid upstream handoffs.

`ci.yml` runs a complete non-publishing qualification for pull requests whose
base is `main`, and the production lifecycle on its six-hour schedule or an
exact-main manual dispatch. A manual
dispatch from any other branch or tag fails before source discovery or
compilation. Ordinary pushes do not start this workflow. The workflow has no
`pull_request_target` or `workflow_run` bridge. CI never deletes software from
the hosted VM. Each disposable flavor runner has one reviewable privileged
setup step that installs QEMU and grants the runner process access to an
existing `/dev/kvm` device; project scripts and all make targets remain
unprivileged. The package transaction suppresses `needrestart` because
restarting or inspecting a hosted runner service has no value on a disposable
VM and can make a successful installation return failure. Missing or unusable
KVM fails each flavor job before compilation; software emulation is not
accepted.

`container-images.yml` owns the toolbox, kernel-build, and minimal APT-client
images as one GHCR bundle. A relevant pull request builds all three with
read-only permissions and no registry login. A relevant canonical-main push,
the Saturday 09:00 UTC schedule, or an exact-main manual dispatch builds all
three before authenticating and moves only their `latest` tags. Publishers are
serialized and never receive delete permission. The final publication check is
an anonymous pull of all three packages.

The `container_images` resolver job resolves the three public `latest` tags,
requires one input fingerprint and publication generation, and emits immutable
digest references. Every image-consuming job selects registry mode explicitly;
the main CI workflow contains no Containerfile build. Pull-request CI resolves
the currently published bundle, while the separate read-only image workflow
verifies candidate Containerfile changes when relevant paths changed. Candidate
images are never published or passed into the main pull-request workflow.

Trusted runs whose typed lifecycle decision requires a build run the `v2`/`v3`
matrix on independent standard `ubuntu-26.04` runners. Every parallel job
builds a self-contained ten-package flavor result, compiles a separately
attested exact-source kernel
selftest bundle, boots the packages in QEMU, and runs that bundle. Changing the
test selection therefore does not rebuild kernel packages. The v2 result
records the two x86 tests that start at the v3
baseline as omitted rather than treating their internal early exit as a pass.
The profile's environment-sensitive cases and maintenance rules are documented
in [`docs/KERNEL_TESTING.md`](../../docs/KERNEL_TESTING.md).

An accepted flavor is saved only after build attestation, selftest compilation,
KVM boot, and guest qualification all pass. Its semantic identity binds the
Debian source version and descriptor hash,
downstream revision, build-policy digest, LTO mode, flavor, and a separate
digest of every qualification input. Container image digests are retained as
provenance inside the sealed cache but do not participate in its identity. APT,
signing, and storage implementation files are deliberately outside that
identity, so a failed downstream run can be corrected without compiling the
same kernel again. Production uses that semantic identity as its exact transport
key. A pull request uses an additional run-and-attempt-qualified transport key,
so it cannot hit an accepted `main` entry and always executes both kernel builds
and both VMs. Its entries remain confined to the pull-request merge ref and are
used only to hand the two results to the package job in that run. There are no
prefix fallback keys. Restored files are treated as
untrusted: a complete SHA-256 inventory and all semantic identities and PASS
records are verified before use. An exact hit skips runner QEMU setup, kernel
compilation, selftest compilation, and VM execution. Flavor packages, source,
and build/VM evidence are not uploaded as workflow artifacts.

A dependent job restores and independently verifies both exact cache entries,
proves the common packages byte-identical, selects their
canonical `v2` copies, reconciles the resulting 18 unique packages, and runs
clean image-only plus complete headers/DKMS clients. It then assembles one
unsigned 19-binary/two-source repository and a strict hashed signing request.
For a pull request, the same package job additionally assembles the repository
with a disposable key and runs the complete signed clean client, including
binary/source acquisition and negative signature cases. Only bounded test
evidence is uploaded; the disposable key and repository are not retained.

Production signing is deliberately separated from both package processing and
final verification. A credential-free gate first proves that the tested commit
is still canonical `main`. The only job attached to the exact-main
`production-signing` Environment repeats that check, receives only the archive
signing-subkey secrets, revalidates the exact unsigned handoff, and requires its
request to match the typed lifecycle decision before the private subkey is
imported. It then creates a bounded signature/state overlay. The
following no-secret job rejects any handoff mismatch, merges the overlay, and
runs the complete clean APT client, including by-hash acquisition, keyring
installation, installation of both release kernels from the signed archive,
source reconstruction and rebuild, signed-state validation, and negative
signature tests. The complete repository and evidence are uploaded as seven-day
workflow artifacts. Only after this job passes can the production-storage job
acquire the shared lease, conditionally publish the exact verified bytes, apply
the complete exact plan derived from signed tombstones within fail-closed safety
caps, enforce the signed whole-namespace byte limit, and verify committed signed
state through a separate read-only credential. A failed downstream stage leaves
the two accepted caches available for a retry. After the final authenticated
state read proves the intended generation, the canonical-main terminal job
deletes both exact `main` cache keys; maintenance and no-op paths perform the
same idempotent cleanup.
Its token has `actions: write` only for this cleanup boundary. A later no-op
retry can finish an interrupted deletion without republishing.

Both build and metadata-maintenance publication paths depend on the fast tier.
A failing unit, schema, type, shell, Make, or workflow-structure check therefore
prevents signing and storage mutation even when no kernel compilation is needed.

`verify-repository` is the mandatory boundary before external mutation.
`publish-repository` lists it in `needs` and consumes only the complete
repository artifact it produced. Depending directly on `sign-repository` is
forbidden because a valid signature alone does not prove that a fresh client
can install, update, fetch source, and reject bad metadata.

The `production-signing` Environment has an exact-`main` branch policy but no
required reviewer, wait timer, or custom protection rule. This allows scheduled
discovery runs to complete without manual approval.

Disposable storage qualification and destructive failure injection run only
from a controlled local checkout. The main CI workflow contains the sole hosted
storage path after all build, VM, signing, and clean-client gates. No workflow
contains a separate disposable qualification job.

Pull requests discover authenticated Debian source, run the fast and release
preflight tiers, always build and VM-test v2/v3 through isolated cache transport,
run the package matrix, and exercise a disposable signed repository. Their typed
qualification has no publication authority; authoritative-state reads,
production signing, publication, and final state verification remain skipped.
Jobs that cross those deliberately skipped production-only branches use
`always()` together with explicit successful-result checks for every direct
producer. This prevents GitHub's skipped-ancestor propagation from suppressing
the pull-request build while keeping genuine dependency failures fail-closed.
Trusted runs additionally direct-read signed authoritative state. They build
only for a newer source or an explicitly increased downstream revision. They
refresh metadata when `Valid-Until` enters its safety horizon,
when the signed retention policy changes, or when the measured namespace is
over its signed byte limit; otherwise they finish as a typed no-op. The first
hosted generation is accepted. The unattended schedule is active, ordinary
pushes cannot authorize the lifecycle, and exact-main manual dispatch remains
available for recovery. Production setup is documented in
[`docs/MAINTAINER_SETUP.md`](../../docs/MAINTAINER_SETUP.md). There is no paid
runner selector or synthetic CPU/RAM cap.

The hosted production path has completed real cache misses, saves, publication,
authenticated no-op verification, and exact final cache deletion. A public APT
endpoint is published and accepted without adding a delivery credential or
control-plane call to CI. The exact lifecycle and delivery evidence is recorded
in [`docs/VALIDATION.md`](../../docs/VALIDATION.md).
