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

## Adding a series

1. Create `config/source-profiles/<id>/` with both TOML files, starting from the
   closest existing series and reviewing every value against the new source.
2. Run `make overlay-patches` with the new source, which regenerates only that
   profile's overlay through `scripts/in-container/generate-overlay-patches.py`.
   Generation fails and names the anchor when an edit no longer applies; add the
   new reviewed spelling to the generator rather than letting a patch apply with
   fuzz.
3. Run `make release-preflight` against the new source.
4. Build, attest, and qualify one flavor, then reconcile `profile.toml` with the
   exact SIMD and FPU findings that the build reports.
