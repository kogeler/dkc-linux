# Flavor policy

One TOML file per flavor (`v2`, `v3`, `v4`) is the machine-readable source of
truth for its psABI micro-architecture baseline, Kconfig symbol, Rust target,
CPU compatibility flags, and the reviewed no-SIMD flags.

`intentional-fpu-objects.toml` is an exact, source-versioned list of C objects
that the kernel itself compiles with `CC_FLAGS_FPU`. It deliberately contains
no directory globs. A new or removed object therefore fails the command audit
and requires review. Its final-module symbols are derived from those exact
objects during the build, then reconciled against the disassembled module.

`intentional-simd-symbols.toml` contains the other exact final-artifact/symbol
pairs reviewed from Linux 7.1.7: optimized crypto/RAID implementations, KVM
guest-FPU access, and explicitly bracketed display copies. The full audit
streams `vmlinux` and every shipped module through the selected
`llvm-objdump-21`; directory and symbol globs are unsupported. Detection covers
vector, mask, AMX tile, MPX bound, MMX and x87 registers plus implicit FPU/state
operations such as `emms`, `ldmxcsr`, `xsave*`, `xrstor*`, and `vzero*`. The
single reviewed alternatives section is exact and all other matches require an
exact final symbol.

Saved Kbuild commands are checked separately. Ordinary C and Rust targets must
receive exactly the selected baseline followed by the final no-SIMD controls;
host tools must not inherit the target baseline. Boot, real-mode, and EFI-stub
C/assembly targets use their specialized upstream flag sets and are rejected if
the DKC flavor `-march` leaks into them. Explicit assembly remains source-driven
and the final linked machine-code audit covers the normal kernel and modules.

A flavor is a real compiler baseline, not a label: the invariant that a higher
integer/CPUID baseline must never permit accidental compiler-generated SIMD in
ordinary kernel code is enforced by an audit, not by assumption.
