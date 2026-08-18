# Installing DKC

The production repository at `https://dkc-linux.romancello.net` has been
clean-client tested, signed, published, and independently verified. The
acceptance is recorded in [VALIDATION.md](VALIDATION.md).

The implemented repository targets Debian 13 (`trixie`) on `amd64`. Its
automatic release set contains the `v2` and `v3` CPU flavors. The implemented
`v4` flavor is retained for periodic manual qualification but is not
distributed; see the [release flavor policy](BUILD.md#release-flavor-policy).

## Important limitation

**DKC kernels do not boot while UEFI Secure Boot enforcement is enabled.** The
APT archive signature authenticates downloaded metadata and packages; it does
not grant UEFI, kernel, or module-signing trust. Keep Debian's stock kernel
installed as a known-good fallback. See [SECURITY.md](SECURITY.md).

## Install the archive trust root

The following single copy-and-paste command downloads the public keyring,
verifies its primary fingerprint, creates APT's local keyring directory when
needed, and installs the verified file:

```sh
(
  set -eu
  keyring="$(mktemp)"
  trap 'rm -f "$keyring"' EXIT
  curl -fsSLo "$keyring" \
    https://dkc-linux.romancello.net/keys/dkc-archive-keyring.gpg
  primary_fingerprints="$(gpg --show-keys --with-colons "$keyring" | \
    awk -F: '$1 == "pub" { want_fpr = 1; next } \
      want_fpr && $1 == "fpr" { print $10; want_fpr = 0 }')"
  test "$primary_fingerprints" = \
    7B98D4BE13418D38BAC037D276349629CC453C26
  sudo install -d -m 0755 /etc/apt/keyrings
  sudo install -m 0644 "$keyring" \
    /etc/apt/keyrings/dkc-archive-keyring.gpg
)
```

Compare the fingerprint through an independently trusted project channel before
the first installation. Then create the Deb822 source:

```sh
APT_BASE_URL=https://dkc-linux.romancello.net
sudo tee /etc/apt/sources.list.d/dkc.sources >/dev/null <<EOF
Types: deb deb-src
URIs: ${APT_BASE_URL}
Suites: trixie
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/dkc-archive-keyring.gpg
EOF
sudo apt update
```

The active keyring is local administrator state under `/etc/apt/keyrings`.
Archive package upgrades do not replace it. If the published fingerprint ever
changes, verify the new fingerprint through an independently trusted project
channel and repeat the command above before accepting metadata signed only by
the new key.

## Enable Debian backports when installing headers

Kernel images need no extra Debian suite. DKC headers depend on the matching
LLVM 21 tools from the official Debian 13 backports suite. If
`trixie-backports` is not already configured, add it before installing a
headers metapackage:

```sh
sudo tee /etc/apt/sources.list.d/trixie-backports.sources >/dev/null <<'EOF'
Types: deb
URIs: https://deb.debian.org/debian
Suites: trixie-backports
Components: main
Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg
EOF
sudo apt update
```

This is an official Debian source authenticated by Debian's archive keyring;
do not substitute Sid or a third-party LLVM repository.

## Choose a CPU flavor

Use the lowest flavor that provides the compatibility you need:

- `v2` is the widest DKC release baseline, but still requires every online CPU
  to implement the x86-64-v2 feature set;
- `v3` requires every online CPU to implement x86-64-v3. It is appropriate only
  when the machine, all hot-pluggable CPUs, and any migration destination meet
  that baseline.

From a checkout of this repository, the unprivileged selector checks every CPU
listed in `/proc/cpuinfo`:

```sh
./scripts/dkc-cpu-select --require v2
./scripts/dkc-cpu-select --require v3
```

The command exits nonzero when any online CPU lacks the requested baseline. The
helper is a repository maintenance tool and is not installed by the kernel
packages.

## Install the kernel and headers

For `v2`:

```sh
sudo apt install dkc-linux-image-v2-amd64 dkc-linux-headers-v2-amd64
```

For `v3`:

```sh
sudo apt install dkc-linux-image-v3-amd64 dkc-linux-headers-v3-amd64
```

These stable metapackages pull the exact versioned base, binary, modules, image,
headers-common, Kbuild, and flavor-header packages. Keep the metapackages
installed: they are how a later repository generation upgrades the selected
flavor.

To install one exact build without following later DKC generations, select a
versioned `dkc-linux-image-<release>-v2-amd64` or
`dkc-linux-image-<release>-v3-amd64` package instead of the stable image
metapackage. Do the same with the matching versioned headers package when
headers are needed. Its dependencies still bring in the required base, binary,
modules, headers-common, and Kbuild packages, but APT will not move that exact
package name to a newer kernel release.

Reboot and select the new DKC entry from GRUB's advanced menu for the first
boot. After login, verify the running release and the installed metapackages:

```sh
uname -r
dpkg-query -W 'dkc-linux-image-*-amd64' 'dkc-linux-headers-*-amd64'
```

The `uname -r` value for a DKC kernel ends in `-v2-amd64` or `-v3-amd64`.

## Upgrades

Normal APT upgrades follow the installed stable metapackages:

```sh
sudo apt update
sudo apt upgrade
```

Do not remove an old working kernel until the new one has booted successfully.
DKC package names are separate from Debian's stock `linux-image-*` packages, so
the fallback can remain installed alongside them.

## Roll back or remove DKC

If a DKC kernel fails, reboot and choose a Debian stock kernel from GRUB's
advanced menu. Confirm that `uname -r` is no longer a DKC release before
removing packages.

For example, remove the `v3` upgrade roots and then their now-unused versioned
dependencies:

```sh
sudo apt remove --purge \
  dkc-linux-image-v3-amd64 dkc-linux-headers-v3-amd64 \
  dkc-linux-base-v3-amd64
sudo apt autoremove --purge
```

Replace `v3` with `v2` for that flavor. Review APT's proposed removals before
confirming them. Removing the source and its locally managed keyring is optional
and should be done only when the machine will no longer consume the repository:

```sh
sudo rm -f \
  /etc/apt/sources.list.d/dkc.sources \
  /etc/apt/keyrings/dkc-archive-keyring.gpg
sudo apt update
```

## Retrieve the corresponding source

Because the repository publishes `deb-src` beside the binaries, APT can
reconstruct the exact downstream Debian source package:

```sh
apt-get source dkc-linux
```

The signed repository also contains the source package for
`dkc-archive-keyring`. Build identity, source reconstruction, and licensing are
documented in [BUILD.md](BUILD.md) and
[LICENSES/README.md](../LICENSES/README.md).
