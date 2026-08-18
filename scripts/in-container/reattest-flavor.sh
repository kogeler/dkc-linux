#!/usr/bin/env bash
# Replay Kbuild and SIMD attestation from a completed compilation result.

set -Eeuo pipefail

[ "$#" -eq 5 ] || {
	echo "usage: reattest-flavor.sh <result> <output> <llvm-major> <v2|v3|v4> <observations|full>" >&2
	exit 2
}

result="$1"
output="$2"
llvm_major="$3"
flavor="$4"
mode="$5"
[[ "$llvm_major" =~ ^[0-9]+$ ]] || {
	echo "invalid LLVM major" >&2
	exit 2
}
[[ "$flavor" =~ ^v[234]$ ]] || {
	echo "invalid flavor" >&2
	exit 2
}
case "$mode" in observations | full) ;; *)
	echo "invalid replay mode" >&2
	exit 2
	;;
esac

artifacts="$result/artifacts"
evidence="$result/evidence"
replay="$evidence/attestation-replay"
for path in \
	"$artifacts" \
	"$evidence/evidence.sha256" \
	"$evidence/kbuild-commands.tsv.xz" \
	"$evidence/publication-identity.json" \
	"$replay/derived-fpu-symbols.json" \
	"$replay/build-tree-inventory.json" \
	"$replay/executable-sections.json" \
	"$replay/manifest.json" \
	"$replay/System.map" \
	"$replay/vmlinux.zst"; do
	test -e "$path" || {
		echo "replay input is absent: $path" >&2
		exit 1
	}
done
if [ "$mode" = observations ]; then
	test -f "$replay/kernel-simd-observations.json.xz" || {
		echo "replay input is absent: $replay/kernel-simd-observations.json.xz" >&2
		exit 1
	}
fi

(
	cd "$evidence"
	sha256sum --check evidence.sha256 >/dev/null
)

if [ -f "$evidence/artifacts.sha256" ]; then
	artifact_manifest="$evidence/artifacts.sha256"
elif [ -f "$evidence/failure-artifacts.sha256" ]; then
	artifact_manifest="$evidence/failure-artifacts.sha256"
else
	echo "result has no artifact checksum manifest" >&2
	exit 1
fi
(
	cd "$artifacts"
	sha256sum --check "$artifact_manifest" >/dev/null
)

mkdir -p "$output/evidence" /work/replay
python3 - "$replay/manifest.json" "$replay/vmlinux.zst" \
	"$replay/System.map" "$replay/config" "$replay/executable-sections.json" \
	"$evidence/kbuild-commands.tsv.xz" "$replay/build-tree-inventory.json" \
	"$llvm_major" <<'PY'
import hashlib
import json
import pathlib
import sys


def digest(path: pathlib.Path) -> tuple[int, str]:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return path.stat().st_size, value.hexdigest()


(
    manifest_path,
    compressed,
    system_map,
    config,
    executable_sections,
    kbuild_commands,
    build_tree_inventory,
    llvm_major,
) = sys.argv[1:]
manifest = json.load(open(manifest_path, encoding="utf-8"))
if (
    manifest.get("schema_version") != 2
    or manifest.get("status") != "COMPLETE"
    or manifest.get("llvm_major") != int(llvm_major)
    or manifest.get("executable_sections_comparison") != "PASS"
):
    raise SystemExit("attestation replay manifest identity is invalid")
for key, name in (
    ("vmlinux_zst", compressed),
    ("system_map", system_map),
    ("config", config),
    ("executable_sections", executable_sections),
    ("kbuild_commands", kbuild_commands),
    ("build_tree_inventory", build_tree_inventory),
):
    size, sha256 = digest(pathlib.Path(name))
    if manifest.get(key) != {"size": size, "sha256": sha256}:
        raise SystemExit(f"attestation replay input differs: {key}")
PY

identity="$evidence/publication-identity.json"
lto_mode="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lto_mode"])' \
	"$identity")"
case "$lto_mode" in none | thin | full) ;; *)
	echo "publication identity has an invalid LTO mode" >&2
	exit 1
	;;
esac

zstd -q -d -c "$replay/vmlinux.zst" >/work/replay/vmlinux
python3 /work/repo/scripts/in-container/attest-one-build.py \
	/work/replay/vmlinux "$artifacts" "$output/evidence" "$llvm_major" \
	"$identity" "$flavor" "$lto_mode" \
	--replay-evidence "$evidence"

python3 /work/repo/scripts/in-container/audit-kbuild-commands.py \
	"$output/evidence/kbuild-commands.tsv.xz" \
	"/work/repo/config/flavors/${flavor}.toml" \
	"$output/evidence/kbuild-command-audit.json" "$lto_mode"

simd_common=(
	"$artifacts"
	/work/repo/config/flavors/intentional-simd-symbols.toml
	"$output/evidence/kernel-simd-audit.json"
	"$llvm_major"
	--lto-mode "$lto_mode"
	--derived-fpu-inventory "$replay/derived-fpu-symbols.json"
)
if [ "$mode" = observations ]; then
	python3 /work/repo/scripts/in-container/audit-kernel-simd.py \
		/dev/null "${simd_common[@]}" \
		--observations-input "$replay/kernel-simd-observations.json.xz"
else
	python3 /work/repo/scripts/in-container/audit-kernel-simd.py \
		/work/replay/vmlinux "${simd_common[@]}" \
		--system-map "$replay/System.map" \
		--observations-output "$output/evidence/kernel-simd-observations.json.xz"
fi

cat >"$output/evidence/result.env" <<EOF
status=PASS
scope=post-build-reattest
flavor=${flavor}
mode=${mode}
publishable=false
EOF
cp "$evidence/publication-identity.json" "$output/evidence/"
cp "$evidence/evidence.sha256" "$output/evidence/input-evidence.sha256"
cp "$replay/manifest.json" "$output/evidence/replay-input-manifest.json"
(
	cd "$output/evidence"
	find . -type f ! -name evidence.sha256 -print0 |
		sort -z | xargs -0 sha256sum
) >"$output/evidence/evidence.sha256"

echo "post-build reattestation PASS (${mode})" >&2
