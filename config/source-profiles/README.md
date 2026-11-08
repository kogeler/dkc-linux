# Debian source compatibility profiles

One profile per upstream kernel series holds everything DKC assumes about that
series. Adding a series never rewrites the reviewed assumptions an older series
still needs, so an older Debian source stays buildable after a newer one lands.

```
config/source-profiles/<id>/profile.toml      build inputs (publication identity)
config/source-profiles/<id>/validation.toml   acceptance inputs (qualification)
debian-overlay/patches/<id>/*.patch           GPL-2.0 packaging overlay
tests/integration/kselftest-patches/<id>/     optional GPL-2.0 selftest patches
```

`<id>` is the upstream `X.Y` series. The split between the two TOML files is
deliberate: `profile.toml` is hashed into the kernel publication identity and is
embedded in the published source package, while `validation.toml` decides
whether a built flavor is accepted. Revising a selftest selection therefore
invalidates qualified results without changing what was built.

## What lives in a profile

| File | Content |
| --- | --- |
| `profile.toml` | Debian build profiles, Debian's kernel architecture inventory, the exact `CC_FLAGS_FPU` objects, and the reviewed final-artifact SIMD symbols |
| `validation.toml` | Saved-Kbuild-command coverage floors and LTO exceptions, and the bounded exact-source kselftest profile |
| `debian-overlay/patches/<id>/` | The generated packaging overlay for that Debian packaging generation |
| `tests/integration/kselftest-patches/<id>/` | Selftest source fixes the series still needs, if any |

## Selection

Selection is exact and fail-closed. A Debian source version must be covered by
exactly one profile; a series without a profile stops the lifecycle with the
version it could not place instead of reusing a neighbouring series' policy.

Optional `first_source_version` and `before_source_version` bounds split one
series when Debian changes its packaging inside it. Bounds of the same series
must not overlap.

The registry includes Linux 7.1, 7.2, and 7.3. The 7.3 profile starts from
Debian `linux` 7.3.3-1: it retains the Debian 13 image-layout overlay, adds the
24 AMD display FPU objects introduced since 7.2, and carries only the futex
atomic-assert selftest fix. The uevent buffer and Landlock character-device
fixes are already in that source. Each profile retains exact object and symbol
inventories; the new display directories are not blanket SIMD exceptions.

## Adding a series

1. Create `config/source-profiles/<id>/` with both TOML files, starting from the
   closest existing series and reviewing every value against the new source.
   Seed `debian-overlay/patches/<id>/` from the same series: registry loading
   requires a complete overlay directory even during regeneration. This seed
   is replaced by the next step and is not evidence of compatibility.
2. Run `make overlay-patches` with the new source, which regenerates only that
   profile's overlay through `scripts/in-container/generate-overlay-patches.py`.
   Generation fails and names the anchor when an edit no longer applies; add the
   new reviewed spelling to the generator rather than letting a patch apply with
   fuzz. Keep each anchor to the lines the edit changes: builds apply these
   anchored edits, so text an anchor does not include may change in a later
   Debian upload of the series without stopping the build.
3. Run `make release-preflight` against the new source.
4. Build, attest, and qualify each published flavor, starting with one flavor
   to reconcile `profile.toml` with its exact SIMD and FPU findings. Run
   `make build-flavor`, `make kselftest-flavor`, and `make qemu-boot-flavor` with
   `FLAVOR=v2`, then repeat for `v3`. Audit exceptions stay exact even when
   upstream Makefiles use directory-wide FPU flags.
5. Run `make package-matrix` and `make github-apt-repository-qualify` to prove
   cross-flavor consistency and the complete disposable-key APT client. Keep
   the source inventory, result directories, and qualification evidence; do not
   substitute static profile checks for build or runtime acceptance.
