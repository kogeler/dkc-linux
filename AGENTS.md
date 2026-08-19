# Agent entry point

Read this file before changing the repository. It is the single routing page
for repository work; detailed policy remains in the linked, versioned
documentation.

## Non-negotiable working contract

- **Do not create a commit unless the operator explicitly commands you to make
  one in the current request.** Editing, fixing, finishing, preparing for a
  push, or passing tests does not authorize `git commit`, `git commit-tree`, an
  amend, a fixup, a squash, or any other commit-producing operation. Leave
  completed changes uncommitted by default and report that state.
- Never push refs or objects. The operator performs every push; no request to
  prepare, finish, publish, or test work authorizes an agent to contact a Git
  remote with a write operation.
- Do not override, shadow, unset, or replace settings supplied by the global
  Git configuration. In particular, never replace the configured author,
  committer, email, signing behavior, hooks, or credentials through repository
  or worktree config, `git -c`, environment variables, or command-line bypass
  flags. Read and use the effective global settings as they are; if they block
  a requested Git operation, stop and ask the operator instead of working
  around them.
- Do not tag, rewrite history, or delete Git data without a separate, explicit
  operator instruction covering that action.
- Inspect `git status` before editing. Preserve existing tracked and untracked
  work, and never discard changes merely because they are unrelated to the
  current task.
- Never commit secrets, print them, place them on a command line, or include
  them in evidence. Treat non-public endpoints, private account identifiers,
  bucket names, and backend-provider details as sensitive infrastructure
  metadata. Follow
  [docs/SECURITY.md](docs/SECURITY.md) and the provider-neutral contract in
  [docs/STORAGE.md](docs/STORAGE.md).
- Keep tracked project content self-contained and written in English. It must
  not refer to private plans, session labels, unavailable documents, or
  automated authorship. This agent-facing file is the sole exception for
  agent-specific instructions.
- Use the repository's Make targets instead of reproducing workflow logic in
  ad-hoc shell snippets. GitHub-only adapters begin with `github-`; ordinary
  local targets must remain usable without `sudo`.
- Report `PASS` only when that exact check ran and passed. Do not infer success
  from an earlier, narrower, or interrupted execution.
- Avoid expensive kernel rebuilds when a saved artifact or a narrower
  attestation/test target can answer the question. Keep diagnostic output
  bounded; do not load entire large build logs when summaries and targeted
  searches are sufficient.

## Start here

1. Read [README.md](README.md) for the product, supported flavors, installation,
   and the contributor overview.
2. Read [docs/README.md](docs/README.md) for the complete documentation map and
   current implementation status.
3. Run `make help` for the canonical command inventory. Do not assume a target
   from prose when the Make interface can be inspected directly.
4. Read only the task-relevant documents below before acting.

## Documentation routes

| Work area | Required references |
| --- | --- |
| User installation, CPU flavor choice, upgrades, rollback | [README.md](README.md), [docs/USER_INSTALL.md](docs/USER_INSTALL.md) |
| Local images, source selection, kernel builds, ThinLTO, attestations | [docs/BUILD.md](docs/BUILD.md), [config/flavors/README.md](config/flavors/README.md) |
| Debian packaging overlay and compatibility | [debian-overlay/README.md](debian-overlay/README.md), [debian-overlay/COMPATIBILITY.md](debian-overlay/COMPATIBILITY.md) |
| KVM boot and kernel selftests | [docs/KERNEL_TESTING.md](docs/KERNEL_TESTING.md) |
| GitHub workflow graph, job handoffs, release caches, hosted capacity | [.github/workflows/README.md](.github/workflows/README.md), [docs/CI_CAPACITY.md](docs/CI_CAPACITY.md) |
| Lifecycle decisions, signing, publication, recovery | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/PUBLISHING.md](docs/PUBLISHING.md) |
| Object storage and retention/GC | [docs/STORAGE.md](docs/STORAGE.md), [docs/RETENTION.md](docs/RETENTION.md) |
| Maintainer environments and archive keys | [docs/MAINTAINER_SETUP.md](docs/MAINTAINER_SETUP.md), [docs/KEYS.md](docs/KEYS.md) |
| Public delivery-cache configuration | [docs/CLOUDFLARE_CACHE.md](docs/CLOUDFLARE_CACHE.md) |
| Trust boundaries and secret handling | [docs/SECURITY.md](docs/SECURITY.md) |
| Evidence and accepted results | [docs/VALIDATION.md](docs/VALIDATION.md) |
| Machine-readable handoffs | [schemas/README.md](schemas/README.md) |
| Licensing and inherited Debian/Linux obligations | [LICENSES/README.md](LICENSES/README.md) |
| Known remaining limitations | [TODO.md](TODO.md) |

## Change and verification flow

1. Resolve the relevant tracked files and current Git state.
2. Read the routed documentation and the implementation/tests that define the
   behavior being changed.
3. Make the smallest coherent change, updating documentation and tests when the
   public contract changes.
4. Run the narrowest relevant checks first, then `make fast` for changes that
   can affect the tracked project contract. Run expensive build, VM, or live
   integration targets only when the task requires their evidence.
5. Review `git diff --check`, the final diff, and `git status`.
6. Report files changed, checks actually run, remaining risks, and that the
   worktree is uncommitted. Commit only if the operator then explicitly asks.
