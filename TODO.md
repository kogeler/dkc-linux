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
