# Debian Kernel Current (DKC)

**The newest kernel source published by Debian, rebuilt for Debian 13 and
modern x86-64 processors. No Sid userspace required.**

Debian stable is delightfully boring. Its kernel does not have to be. DKC keeps
the dependable Debian 13 (`trixie`) userspace while following the newest
authenticated Debian Sid `src:linux` package. It turns that source into ordinary
Debian packages, boots and tests them, and publishes them through a signed APT
repository.

## Why try DKC?

- **A fresher Debian kernel without a FrankenDebian.** You get the newest kernel
  source packaged by the Debian Kernel Team without installing Sid binaries or
  replacing the rest of Trixie.
- **Real `x86-64-v2` and `x86-64-v3` builds.** Debian's official archive does
  not offer its kernel as separately installable v2/v3 flavors. DKC gives LLVM
  an explicit CPU baseline and audits the resulting machine code. Pick `v2` for
  reach or `v3` for newer machines.
- **Clang/LLVM 21 instead of GCC.** Debian's official kernel configuration
  selects GCC 15; DKC compiles and links the kernel proper with Debian-packaged
  Clang 21, LLD, and the matching LLVM tools. That gives the kernel a modern,
  independent optimizer and code generator, unlocks LLVM-native LTO, and is
  verified by auditing every recorded Kbuild command—not by trusting the banner.
- **ThinLTO by default.** ThinLTO lets LLVM optimize across source files while
  keeping the link parallel and practical. This is not purely theoretical:
  Meta reports [non-trivial performance improvements from
  production ThinLTO kernels](https://lpc.events/event/19/contributions/2212/),
  and the Linux LLVM maintainer explains why ThinLTO retains
  [most FullLTO optimization opportunities](https://events.linuxfoundation.org/wp-content/uploads/2024/11/Nathan-Chancellor-Mentorship-Webinar-11-13-24.pdf).
  A late-2025 Linux 6.19/LLVM 21 FullLTO benchmark measured roughly 6% higher
  performance than its GCC baseline across the kernel-sensitive workloads that
  moved, including storage, networking, web serving, databases, and scheduler
  microbenchmarks. Most of its 163 tests barely changed, so treat LTO as a real
  workload-dependent advantage, not magic benchmark dust.
  [See the results.](https://www.phoronix.com/review/linux-kernel-llvm-clang-lto/5)
- **It still behaves like Debian.** Stable metapackages handle upgrades, headers
  support DKMS, stock Debian kernels can stay installed as a fallback, and the
  signed repository includes the exact downstream source package through
  `deb-src`.
- **The expensive checks happen before you install anything.** Every release
  passes package and dependency audits, a complete Kbuild command audit, a
  disassembly-level SIMD audit, direct installation, KVM boot, kernel selftests,
  DKMS/module checks, removal, and fallback to the stock kernel.

The repository is live. An unattended lifecycle checks Debian four times a day
at `00:17`, `06:17`, `12:17`, and `18:17` UTC. A new source version triggers the
build, test, signing, and publication chain; an unchanged version is a cheap
no-op. The accepted production evidence is in
[`docs/VALIDATION.md`](docs/VALIDATION.md).

> **One important catch:** DKC kernels do not boot while UEFI Secure Boot
> enforcement is enabled. Keep Debian's stock kernel installed until the new
> kernel has booted successfully. Secure Boot support is outside the current
> project scope.

## Install in four commands

DKC supports Debian 13 on `amd64`. Stock Trixie already provides
`/etc/apt/keyrings`, so adding the key and repository takes two commands:

```sh
curl -fsSL https://dkc-linux.romancello.net/keys/dkc-archive-keyring.gpg | sudo tee /etc/apt/keyrings/dkc-archive-keyring.gpg >/dev/null
printf '%s\n' 'Types: deb deb-src' 'URIs: https://dkc-linux.romancello.net' 'Suites: trixie' 'Components: main' 'Architectures: amd64' 'Signed-By: /etc/apt/keyrings/dkc-archive-keyring.gpg' | sudo tee /etc/apt/sources.list.d/dkc.sources >/dev/null
sudo apt update
sudo apt install dkc-linux-image-v3-amd64
```

That last command installs the recommended `v3` flavor. Use `v2` instead when
you need wider CPU compatibility or are not certain that every CPU the kernel
may run on supports x86-64-v3:

```sh
sudo apt install dkc-linux-image-v2-amd64
```

The archive's primary fingerprint is
`7B98 D4BE 1341 8D38 BAC0 37D2 7634 9629 CC45 3C26`. The
[complete installation guide](docs/USER_INSTALL.md) includes the stricter
first-use fingerprint check, CPU selection, headers and Debian backports,
exact-version installs, upgrades, rollback, removal, and source retrieval.

### Which flavor?

| Flavor | Choose it when |
| --- | --- |
| `v2` | You want the widest DKC compatibility, or you are unsure. Every CPU the kernel may run on must support x86-64-v2. |
| `v3` | Every local, hot-pluggable, and VM migration-destination CPU supports x86-64-v3. |

Headers are available through `dkc-linux-headers-v2-amd64` and
`dkc-linux-headers-v3-amd64`. They use LLVM 21 from the official Debian
`trixie-backports` suite; kernel images themselves need no extra Debian suite.

There is a maintained `v4` build path, but no published `v4` flavor. Linux keeps
general kernel code out of SIMD registers and runtime-selects its explicit
AVX-512 implementations. DKC's instruction scan found the same 2,342 AVX-512
instructions in the tested v3 and v4 kernels, in the same optimized symbols. A
v3 kernel can therefore use those paths on a capable CPU without making every
machine meet the v4 baseline. The measured rationale is in the
[release flavor policy](docs/BUILD.md#release-flavor-policy).

## Build or contribute

Everything repeatable is a `make` target. Builds and tests run in ephemeral
rootless containers or VMs; local targets do not use `sudo` or install anything
into host system paths.

```sh
make help             # show the complete target list
make doctor           # check the host and write machine-readable evidence
make image            # build the cached local toolbox image
make container-images # build and verify the complete local image bundle
make fast             # run unit, type, shell, language, and Make checks
make shell            # open an ephemeral toolbox shell with the repo mounted
make clean-all        # remove only this repository's labelled scratch resources
```

The host needs rootless Podman, QEMU/KVM, `make`, `git`, `curl`, `jq`, `gpg`, and
coreutils. `make doctor` reports anything missing. GitHub's disposable hosted
VM is the one narrow exception to the no-`sudo` rule: its setup step installs
QEMU and grants the runner access to `/dev/kvm` before unprivileged Make targets
take over.

Start with the [documentation index](docs/README.md) for build internals,
testing, security, publishing, maintenance, and validation. `make help` remains
the exact command-line reference.

## Licensing and source

The root MIT license covers the independently written project automation, not
Linux or Debian-derived packaging. The per-path policy, inherited licenses, and
non-affiliation notice are in [`LICENSES/README.md`](LICENSES/README.md). Every
binary publication includes one complete downstream Debian source package in
the same signed archive.

The project is deliberately strict about its boring bits: production runs only
from canonical `main`, secrets are never committed or placed on command lines,
cleanup is scoped by verified ownership labels, and a check says `PASS` only
after it actually ran and passed. Fresh kernels are exciting enough; the release
transaction does not need to be.
