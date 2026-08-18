# Debian packaging overlay

The minimal, machine-reviewable DKC delta applied to the verified Debian Sid
`src:linux` source.

Rules:

- adapt at the packaging source of truth, never only in a generated
  `debian/control`;
- no silent disablement of Rust, BTF, module signing policy, hardening, or any
  other Debian security feature to make a build succeed;
- every change carries a reason, a security/reproducibility impact note, and a
  revalidation trigger for the next Debian source revision.

The patch series selects the versioned Debian LLVM toolchain, drives every
Kbuild entry point with it, disables the initial product's random module-signing
stage, adds the reviewed x86-64-v2/v3/v4 baselines, and places every generated
kernel binary in the `dkc-linux-*` namespace. The release matrix currently
publishes v2/v3 only. The publication ABI and source version
are derived from the complete build-input digest at build time; they are not
hard-coded into a generated patch.

Binary package names and header payload paths are deliberately separate. The
packages are DKC-namespaced, while their unique installed paths remain the
standard `/usr/src/linux-headers-<KREL>` and
`/usr/src/linux-headers-<DKC_ABI>-common`. This preserves external-module and
DKMS conventions; generated `/lib/modules/<KREL>/build` and `source` links are
audited against those exact targets.

The unique versioned Kbuild package has its own `pkg.dkc.nokbuild` profile.
`dh-python`, which its `binary_kbuild` target invokes, follows that same profile
instead of Debian's broader `pkg.linux.notools`; dependency resolution and the
selected binary target can therefore never disagree about `dh_python3`.
