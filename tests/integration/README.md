# Integration tests

Every integration test runs in an ephemeral, labelled environment and removes
only resources carrying its run and owner labels.

The implemented package matrix consumes the accepted v2 and v3 results. It
checks the exact 18-package release graph, rendered maintainer scripts, and
payload ownership, then uses clean Debian 13 clients for image-only dependency
closure and the complete package union. Every installed version must remain
downloadable from an allowed Debian or local-flat-repository origin. Removing
only the v3 stable meta-packages must make APT propose the exact v3 versioned
closure without selecting v2 or the Debian fallback kernel.
The controlled `dkms-fixture` package is installed before DKC so real kernel and
header hooks must discover it; its build logs must show the versioned Clang and
LLD selected by the installed headers, and the resulting module vermagic must
match every DKC KREL.

The direct-QEMU tier consumes one retained flavor result and a pinned,
checksum-verified Debian 13 genericcloud image. Each scenario uses a fresh
qcow2 overlay, a read-only input ISO, and an independent result disk. The input
contains one flavor's ten attested `.deb` files, a controlled DKMS fixture, and
a separately built exact-source kernel selftest bundle. The guest installs the
files directly with `dpkg`; it does not create an APT repository or use a test
signing key. It boots the exact DKC release on a recorded CPU model, exercises
the bounded selftest profile, returns to the stock kernel, and removes the DKC
packages. Result files and compressed serial output are retained; raw result
disks and root overlays are destroyed. The profile, its environment-sensitive
fixtures, and the required evidence are documented in
[`docs/KERNEL_TESTING.md`](../../docs/KERNEL_TESTING.md).

These are separate validation layers within one production lifecycle. QEMU
installs one flavor directly and exercises that kernel; it never constructs an
APT repository. The package and signed-repository clients prove dependency,
DKMS, source, and apt-secure behavior in rootless containers. Only the complete
signed repository that passes those no-secret client gates can enter the later
conditional object-storage publication layer.
