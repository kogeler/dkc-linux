# Pending work

## Periodic v4 qualification

The implemented v4 flavor is not part of the automatic release matrix or
binary distribution. Linux already ships its manually optimized AVX-512
crypto, RAID6, and checksum paths in the v3 build and selects them at runtime
on capable hardware. Keeping a third always-on build would therefore add cost
and a stricter boot baseline without currently demonstrated release value.

- Periodically build, attest, boot, and run the complete selftest profile for
  v4 on a KVM host that faithfully exposes the x86-64-v4 baseline.
- Fix v4-specific regressions so the dormant flavor remains straightforward to
  return to CI and distribution if measured benefits justify it.
- Re-enable v4 only together with its KVM gate, package-matrix input, repository
  inventory, clean-client tests, and documentation.
- Do not configure or select a self-hosted runner for this project yet.
- Keep software-emulated VM results outside the release qualification path.

## Linux 7.2 machine-code policy confirmation

The `7.2` source profile carries the reviewed Linux 7.1 final-artifact SIMD
symbols with the artifact paths of 7.2, where RAID6 moved from `lib/raid6` to
`lib/raid/raid6`, plus the three AMD display FRL translation units that 7.2 adds
to the exact `CC_FLAGS_FPU` object list. Both lists are derived from the 7.2
sources, not yet from a 7.2 reference build.

- Build and attest one 7.2 flavor, then reconcile `simd_allowlist` with the
  exact unexpected and unused entries the SIMD audit reports.
- Keep every entry exact: the audit fails on a stale entry as loudly as on an
  unreviewed symbol, which is what makes this reconciliation reviewable.
