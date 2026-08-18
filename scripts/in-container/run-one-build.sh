#!/usr/bin/env bash
# Perform one build in an already network-isolated container and attest it.

set -Eeuo pipefail

[ "$#" -eq 8 ] || {
	echo "usage: run-one-build.sh <label> <flavor> <source-version> <dsc-name> <llvm-major> <jobs> <dkc-revision> <none|thin|full>" >&2
	exit 2
}

label="$1" flavor="$2" source_version="$3" dsc_name="$4" llvm_major="$5" jobs="$6"
dkc_revision="$7" lto_mode="$8"
[[ "$label" =~ ^[a-z][a-z0-9-]*$ ]] || {
	echo "unsafe build label" >&2
	exit 2
}
[[ "$source_version" =~ ^[0-9A-Za-z.+:~_-]+$ ]] || {
	echo "unsafe source version" >&2
	exit 2
}
[[ "$dsc_name" =~ ^[A-Za-z0-9][A-Za-z0-9._+~-]*\.dsc$ ]] || {
	echo "unsafe Debian source control filename" >&2
	exit 2
}
[[ "$flavor" =~ ^v[234]$ ]] || {
	echo "invalid flavor" >&2
	exit 2
}
[[ "$llvm_major" =~ ^[0-9]+$ && "$jobs" =~ ^[1-9][0-9]*$ && "$dkc_revision" =~ ^[1-9][0-9]*$ ]] || {
	echo "invalid numeric argument" >&2
	exit 2
}
case "$lto_mode" in
none | thin | full) ;;
*)
	echo "kernel LTO mode must be none, thin, or full" >&2
	exit 2
	;;
esac

available_cpus="$(nproc)"
[[ "$available_cpus" =~ ^[1-9][0-9]*$ ]] || {
	echo "cannot determine available CPUs" >&2
	exit 1
}
read -r cpu_quota_us cpu_period_us </sys/fs/cgroup/cpu.max || {
	cpu_quota_us=unknown
	cpu_period_us=unknown
}
for cgroup_file in \
	memory.current memory.stat memory.high memory.max memory.peak memory.events \
	memory.pressure memory.swap.current memory.swap.max; do
	test -r "/sys/fs/cgroup/${cgroup_file}" || {
		echo "capacity run lacks cgroup v2 ${cgroup_file}" >&2
		exit 1
	}
done

interfaces="$(find /sys/class/net -mindepth 1 -maxdepth 1 -printf '%f\n' | sort | paste -sd, -)"
[ "$interfaces" = lo ] || {
	echo "offline build unexpectedly has network interfaces: ${interfaces}" >&2
	exit 1
}

work=/work/build
prepared_source="$work/debian-source"
source="$work/dkc-linux-source"
results="/work/results/${label}"
artifacts="$results/artifacts"
evidence="$results/evidence"

test ! -e "$work" || {
	echo "stale build directory exists" >&2
	exit 1
}
mkdir -p "$work" "$artifacts" "$evidence" /work/home

record_controller_error() {
	local error_rc="$1" error_line="$2"
	set +e
	cat >"$evidence/controller-error.env" <<EOF
exit_code=${error_rc}
script_line=${error_line}
EOF
	echo "[$label] build controller failed at line ${error_line}, rc=${error_rc}" >&2
	exit "$error_rc"
}
trap 'record_controller_error "$?" "$LINENO"' ERR

echo "[$label] extracting the verified source inventory with network disabled" >&2
dpkg-source -x "/work/inputs/$dsc_name" "$prepared_source" >/dev/null
for overlay in /work/repo/debian-overlay/patches/*.patch; do
	patch -d "$prepared_source" -p1 --batch --forward --silent <"$overlay"
done
python3 /work/repo/scripts/in-container/prepare-build-identity.py \
	"$prepared_source" /work/repo /work/inputs "$dkc_revision" "$lto_mode"
/work/repo/scripts/in-container/build-source-package.sh \
	"$prepared_source" /work/repo /work/inputs /work/source-package \
	"$source" "$evidence/source-package"
python3 "$source/debian/dkc/prepare-flavor.py" "$source" "$flavor" "$lto_mode"

# shellcheck disable=SC1091,SC2153  # final source-package policy file
. "$source/debian/dkc/build-profiles"
# Every parallel flavor build exports its own common headers and Kbuild
# packages.  This keeps header, DKMS, and VM validation self-contained.  The
# later matrix gate proves the three common copies byte-identical before it
# selects one canonical copy for the flat repository.
build_scope=binary
dh_listpackages_args=()
# shellcheck disable=SC2153  # assigned by the sourced build-profiles file
export DEB_BUILD_PROFILES="$DKC_BUILD_PROFILES"
export SOURCE_DATE_EPOCH
SOURCE_DATE_EPOCH="$(dpkg-parsechangelog -l"$source/debian/changelog" -STimestamp)"
# The publication inventory deliberately contains no automatically generated
# debug-symbol packages.  Debian's kernel rules already pass the equivalent
# dh_strip option for most kernel payloads, but the versioned Kbuild helpers
# contain ELF host tools and otherwise acquire an implicit *-dbgsym package.
# Use debhelper's source-wide build option so every selected binary target has
# the same policy, including targets added by future Debian revisions.
export DEB_BUILD_OPTIONS="parallel=${jobs} noautodbgsym"
export HOME=/work/home

cd "$source"
# The source intentionally fails the next automated build when an input changed
# without refreshing control.md5sum. Our one-flavor transform is such an input,
# so use Debian's explicit maintainer target once before dpkg-buildpackage.
make -f debian/rules debian/control-real >/dev/null
dpkg-checkbuilddeps debian/control

# Do not let Kconfig silently disable Rust when Debian or upstream changes a
# minimum.  Check the source's own minima against the exact locked tools before
# configuring or compiling, and retain the result as evidence.
tool_minimums="$evidence/tool-minimums.env"
: >"$tool_minimums"
for tool in \
	"clang-${llvm_major}" "clang++-${llvm_major}" "ld.lld-${llvm_major}" \
	"llvm-ar-${llvm_major}" "llvm-nm-${llvm_major}" \
	"llvm-objcopy-${llvm_major}" "llvm-objdump-${llvm_major}" \
	"llvm-readelf-${llvm_major}" "llvm-strip-${llvm_major}" \
	"llvm-link-${llvm_major}" rustc bindgen; do
	expected_tool="/usr/bin/${tool}"
	actual_tool="$(command -v "$tool" || true)"
	if [ -z "$actual_tool" ] || [ ! -e "$expected_tool" ] ||
		[ "$(readlink -f "$actual_tool")" != "$(readlink -f "$expected_tool")" ]; then
		echo "tool ${tool} does not resolve through ${expected_tool}" >&2
		exit 1
	fi
done
check_tool_minimum() {
	local name="$1" actual="$2" minimum
	minimum="$(scripts/min-tool-version.sh "$name")"
	if [ -z "$minimum" ] || [ -z "$actual" ]; then
		echo "cannot determine ${name} actual/minimum version" >&2
		exit 1
	fi
	dpkg --compare-versions "$actual" ge "$minimum" || {
		echo "${name} ${actual} is older than kernel minimum ${minimum}" >&2
		exit 1
	}
	printf '%s_actual=%s\n%s_minimum=%s\n' "$name" "$actual" "$name" "$minimum" >>"$tool_minimums"
}
rustc_actual="$(rustc --version | sed -n 's/^rustc \([^[:space:]]*\).*/\1/p')"
bindgen_actual="$(bindgen --version | sed -n 's/^bindgen \([^[:space:]]*\).*/\1/p')"
llvm_actual="$("clang-${llvm_major}" --version | sed -n '1s/.*version \([0-9][^[:space:]]*\).*/\1/p')"
check_tool_minimum rustc "$rustc_actual"
check_tool_minimum bindgen "$bindgen_actual"
check_tool_minimum llvm "$llvm_actual"

selected_packages="$(dh_listpackages "${dh_listpackages_args[@]}")"
printf '%s\n' "$selected_packages" >"$evidence/selected-packages.txt"
python3 - /work/inputs/publication-identity.json "$flavor" "$evidence/selected-packages.txt" <<'PY'
import json
import pathlib
import sys

identity_path, flavor, selected_path = sys.argv[1:]
identity = json.load(open(identity_path, encoding="utf-8"))
names = identity.get("package_names")
abi = identity.get("abi")
if not isinstance(names, dict) or not isinstance(abi, str):
    raise SystemExit("malformed package inventory in publication identity")
versioned = names.get("versioned")
meta = names.get("meta")
if not isinstance(versioned, list) or not isinstance(meta, list):
    raise SystemExit("malformed versioned/meta package inventory")
all_names = versioned + meta
if not all(isinstance(name, str) for name in all_names):
    raise SystemExit("non-string package in publication identity")
expected = {name for name in all_names if name.endswith(f"-{flavor}-amd64")}
expected.update(
    {f"dkc-linux-headers-{abi}-common", f"dkc-linux-kbuild-{abi}"}
)
selected_lines = pathlib.Path(selected_path).read_text(encoding="utf-8").splitlines()
if len(selected_lines) != len(set(selected_lines)) or set(selected_lines) != expected:
    raise SystemExit(
        "selected binary target set differs before compilation: "
        f"missing={sorted(expected - set(selected_lines))}, "
        f"unexpected={sorted(set(selected_lines) - expected)}"
    )
PY
if grep -q -- '-di$' <<<"$selected_packages"; then
	echo "installer packages remained selected despite the noudeb profiles" >&2
	exit 1
fi
if grep -q -- '-dbg$' <<<"$selected_packages"; then
	echo "debug packages remained selected despite pkg.linux.nokerneldbg" >&2
	exit 1
fi
if grep -q '^dkc-linux-kbuild-' <<<"$selected_packages"; then
	command -v dh_python3 >/dev/null || {
		echo "selected Kbuild package requires the undeclared/uninstalled dh_python3 helper" >&2
		exit 1
	}
else
	echo "${flavor} did not select its common Kbuild package" >&2
	exit 1
fi

# Fail cheap policy mistakes before spending ~15 minutes compiling. This uses
# the same two Debian config fragments, highest-precedence overrides, selected
# LLVM, and upstream olddefconfig step as rules.real's setup target.
config_preflight=/work/config-preflight
mkdir -p "$config_preflight"
expected_krel="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["kernel_releases"][sys.argv[2]])' \
	/work/inputs/publication-identity.json "$flavor")"
debian/bin/kconfig.py "$config_preflight/.config" \
	debian/config/config debian/config/amd64/config \
	"debian/config/amd64/config.${flavor}-amd64" \
	-o "BUILD_SALT=\"${expected_krel}\"" \
	-o MODULE_SIG=n
make LLVM="-${llvm_major}" ARCH=x86 O="$config_preflight" olddefconfig >/dev/null
grep -qx '# CONFIG_MODULE_SIG is not set' "$config_preflight/.config" || {
	echo "module signing remained enabled after final olddefconfig" >&2
	exit 1
}
grep -qx 'CONFIG_CC_IS_CLANG=y' "$config_preflight/.config"
grep -qx 'CONFIG_AS_IS_LLVM=y' "$config_preflight/.config"
grep -qx 'CONFIG_LD_IS_LLD=y' "$config_preflight/.config"
grep -qx 'CONFIG_RUST=y' "$config_preflight/.config" || {
	echo "Rust support was silently disabled by Kconfig" >&2
	exit 1
}
grep -qx "CONFIG_DKC_X86_64_BASELINE_${flavor^^}=y" "$config_preflight/.config"
[ "$(grep -Ec '^CONFIG_DKC_X86_64_BASELINE_(V2|V3|V4)=y$' "$config_preflight/.config")" -eq 1 ]
case "$lto_mode" in
none)
	grep -qx 'CONFIG_LTO_NONE=y' "$config_preflight/.config"
	grep -qx '# CONFIG_LTO_CLANG_FULL is not set' "$config_preflight/.config"
	grep -qx '# CONFIG_LTO_CLANG_THIN is not set' "$config_preflight/.config"
	grep -qx 'CONFIG_DEBUG_INFO_BTF=y' "$config_preflight/.config"
	grep -qx 'CONFIG_DEBUG_INFO_BTF_MODULES=y' "$config_preflight/.config"
	;;
thin)
	grep -qx '# CONFIG_LTO_NONE is not set' "$config_preflight/.config"
	grep -qx '# CONFIG_LTO_CLANG_FULL is not set' "$config_preflight/.config"
	grep -qx 'CONFIG_LTO_CLANG_THIN=y' "$config_preflight/.config"
	grep -qx '# CONFIG_DEBUG_INFO_BTF is not set' "$config_preflight/.config"
	! grep -Eq '^CONFIG_DEBUG_INFO_BTF_MODULES=[ym]$' "$config_preflight/.config"
	;;
full)
	grep -qx '# CONFIG_LTO_NONE is not set' "$config_preflight/.config"
	grep -qx 'CONFIG_LTO_CLANG_FULL=y' "$config_preflight/.config"
	grep -qx '# CONFIG_LTO_CLANG_THIN is not set' "$config_preflight/.config"
	grep -qx '# CONFIG_DEBUG_INFO_BTF is not set' "$config_preflight/.config"
	! grep -Eq '^CONFIG_DEBUG_INFO_BTF_MODULES=[ym]$' "$config_preflight/.config"
	;;
esac
cp "$config_preflight/.config" "$evidence/config-preflight"
sha256sum "$evidence/config-preflight" >"$evidence/config-preflight.sha256"

start_epoch="$(date +%s)"
root_used_before="$(df --block-size=1 --output=used /work | tail -1 | tr -d ' ')"
metrics="$evidence/resources.tsv"
printf '%s\n' \
	$'elapsed_seconds\troot_used_bytes\tcgroup_memory_bytes\tcgroup_working_set_bytes\tcgroup_anon_bytes\tcgroup_file_bytes\tcgroup_inactive_file_bytes\tcgroup_active_file_bytes\tcgroup_slab_reclaimable_bytes\tcgroup_slab_unreclaimable_bytes\tcgroup_memory_pressure_some_total_usec\tcgroup_memory_pressure_full_total_usec' \
	>"$metrics"
last_root_sample_elapsed=-30
last_root_used=0
root_sample_errors=0

sample_resources() {
	local force_root="${1:-false}"
	local elapsed root_used memory_current memory_working_set
	local memory_anon memory_file memory_inactive_file memory_active_file
	local memory_slab_reclaimable memory_slab_unreclaimable
	local pressure_some_total pressure_full_total key value class pressure_total _
	local sampled_root_used
	elapsed=$(($(date +%s) - start_epoch))
	if [ "$force_root" = true ] || [ "$((elapsed - last_root_sample_elapsed))" -ge 30 ]; then
		# Filesystem accounting is diagnostic only. A transient observation
		# failure must never terminate an otherwise valid kernel build.
		if sampled_root_used="$(
			df --block-size=1 --output=used /work 2>/dev/null | tail -1 | tr -d ' '
		)" && [[ "$sampled_root_used" =~ ^[0-9]+$ ]]; then
			last_root_used="$sampled_root_used"
		else
			root_sample_errors=$((root_sample_errors + 1))
			echo "[$label] warning: root-usage sample failed; retaining previous value" >&2
		fi
		last_root_sample_elapsed="$elapsed"
	fi
	root_used="$last_root_used"
	memory_current="$(</sys/fs/cgroup/memory.current)"
	memory_anon=0
	memory_file=0
	memory_inactive_file=0
	memory_active_file=0
	memory_slab_reclaimable=0
	memory_slab_unreclaimable=0
	while read -r key value _; do
		case "$key" in
		anon) memory_anon="$value" ;;
		file) memory_file="$value" ;;
		inactive_file) memory_inactive_file="$value" ;;
		active_file) memory_active_file="$value" ;;
		slab_reclaimable) memory_slab_reclaimable="$value" ;;
		slab_unreclaimable) memory_slab_unreclaimable="$value" ;;
		esac
	done </sys/fs/cgroup/memory.stat
	memory_working_set=$((memory_current - memory_inactive_file))
	[ "$memory_working_set" -ge 0 ] || memory_working_set=0
	pressure_some_total=0
	pressure_full_total=0
	while read -r class _ _ _ pressure_total; do
		case "$class" in
		some) pressure_some_total="${pressure_total#total=}" ;;
		full) pressure_full_total="${pressure_total#total=}" ;;
		esac
	done </sys/fs/cgroup/memory.pressure
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
		"$elapsed" "$root_used" "$memory_current" \
		"$memory_working_set" "$memory_anon" "$memory_file" \
		"$memory_inactive_file" "$memory_active_file" \
		"$memory_slab_reclaimable" "$memory_slab_unreclaimable" \
		"$pressure_some_total" "$pressure_full_total" >>"$metrics"
}

sample_resources true
echo "[$label] build start: jobs=${jobs}, LTO=${lto_mode}, SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH}" >&2

/usr/bin/time -v -o "$evidence/time.txt" \
	timeout --signal=TERM --kill-after=5m 5h \
	dpkg-buildpackage --build="$build_scope" --no-sign -j"$jobs" \
	>"$evidence/build.log" 2>&1 &
build_pid=$!
next_progress_elapsed=30
while kill -0 "$build_pid" 2>/dev/null; do
	sleep 5
	if kill -0 "$build_pid" 2>/dev/null; then
		sample_resources
		elapsed="$(tail -1 "$metrics" | cut -f1)"
		if [ "$elapsed" -ge "$next_progress_elapsed" ]; then
			log_bytes="$(stat -c %s "$evidence/build.log" 2>/dev/null || echo 0)"
			root_used="$(tail -1 "$metrics" | cut -f2)"
			memory_bytes="$(tail -1 "$metrics" | cut -f3)"
			working_set_bytes="$(tail -1 "$metrics" | cut -f4)"
			printf '[%s] still building: elapsed=%ss log=%sB root_used=%sB\n' \
				"$label" "$elapsed" "$log_bytes" "$root_used" >&2
			printf '[%s] current container memory: raw=%sB working_set=%sB\n' \
				"$label" "$memory_bytes" "$working_set_bytes" >&2
			next_progress_elapsed=$(((elapsed / 30 + 1) * 30))
		fi
	fi
done
if wait "$build_pid"; then
	build_rc=0
else
	build_rc=$?
fi
sample_resources true
if [ "$build_rc" -ne 0 ]; then
	tail -n 160 "$evidence/build.log" >&2
	echo "[$label] dpkg-buildpackage failed with rc=${build_rc}" >&2
	exit "$build_rc"
fi

final_config="$source/debian/build/build_amd64_none_${flavor}-amd64/.config"
if ! cmp -s "$config_preflight/.config" "$final_config"; then
	diff -u "$config_preflight/.config" "$final_config" >"$evidence/config-preflight-to-final.diff" || true
	echo "[$label] final Kconfig differs from the exact preflight policy" >&2
	exit 1
fi

build_end_epoch="$(date +%s)"
find "$work" -maxdepth 1 -type f \
	\( -name '*.deb' -o -name '*.ddeb' -o -name '*.udeb' \
	-o \( -name '*.buildinfo' ! -name '*_source.buildinfo' \) \
	-o \( -name '*.changes' ! -name '*_source.changes' \) \) \
	-exec cp -t "$artifacts" -- {} +

test -n "$(find "$artifacts" -maxdepth 1 -name '*.deb' -print -quit)" || {
	echo "[$label] build produced no binary packages" >&2
	exit 1
}
test "$(find "$artifacts" -maxdepth 1 -name '*.buildinfo' | wc -l)" -eq 1 || {
	echo "[$label] build did not produce exactly one .buildinfo" >&2
	exit 1
}

/work/repo/scripts/in-container/prepare-attestation-replay.sh \
	"$source/debian/build/build_amd64_none_${flavor}-amd64" \
	"$evidence" "$llvm_major"

if timeout --signal=TERM --kill-after=30s 30m \
	python3 /work/repo/scripts/in-container/audit-kernel-simd.py \
	"$source/debian/build/build_amd64_none_${flavor}-amd64/vmlinux" \
	"$artifacts" \
	/work/repo/config/flavors/intentional-simd-symbols.toml \
	"$evidence/kernel-simd-audit.json" "$llvm_major" \
	--lto-mode "$lto_mode" \
	--system-map "$source/debian/build/build_amd64_none_${flavor}-amd64/System.map" \
	--build-root "$source/debian/build/build_amd64_none_${flavor}-amd64" \
	--fpu-object-policy /work/repo/config/flavors/intentional-fpu-objects.toml \
	--write-derived-fpu-inventory \
	"$evidence/attestation-replay/derived-fpu-symbols.json" \
	--observations-output \
	"$evidence/attestation-replay/kernel-simd-observations.json.xz"; then
	simd_audit_rc=0
else
	simd_audit_rc=$?
fi

timeout --signal=TERM --kill-after=30s 15m \
	python3 /work/repo/scripts/in-container/attest-one-build.py \
	"$source" "$artifacts" "$evidence" "$llvm_major" \
	/work/inputs/publication-identity.json "$flavor" "$lto_mode" &
attestation_pid=$!
while kill -0 "$attestation_pid" 2>/dev/null; do
	sleep 5
	if kill -0 "$attestation_pid" 2>/dev/null; then
		sample_resources
	fi
done
if wait "$attestation_pid"; then
	attestation_rc=0
else
	attestation_rc=$?
fi
sample_resources true
if [ "$attestation_rc" -ne 0 ]; then
	echo "[$label] package attestation failed with rc=${attestation_rc}" >&2
	kbuild_audit_rc=125
elif python3 /work/repo/scripts/in-container/audit-kbuild-commands.py \
	"$evidence/kbuild-commands.tsv.xz" \
	"/work/repo/config/flavors/${flavor}.toml" \
	"$evidence/kbuild-command-audit.json" "$lto_mode"; then
	kbuild_audit_rc=0
else
	kbuild_audit_rc=$?
fi
attestation_end_epoch="$(date +%s)"

timeout --signal=TERM --kill-after=30s 15m \
	lintian --display-info --pedantic --fail-on error "$artifacts"/*.changes \
	>"$evidence/lintian.txt" 2>&1 &
lintian_pid=$!
while kill -0 "$lintian_pid" 2>/dev/null; do
	sleep 5
	if kill -0 "$lintian_pid" 2>/dev/null; then
		sample_resources
	fi
done
if wait "$lintian_pid"; then
	lintian_rc=0
else
	lintian_rc=$?
fi
sample_resources true
if [ "$lintian_rc" -ne 0 ]; then
	tail -n 100 "$evidence/lintian.txt" >&2
	echo "[$label] lintian failed to audit packages, rc=${lintian_rc}" >&2
fi
verification_end_epoch="$(date +%s)"
cat >"$evidence/post-build-gates.env" <<EOF
package_attestation_rc=${attestation_rc}
kbuild_audit_rc=${kbuild_audit_rc}
simd_audit_rc=${simd_audit_rc}
lintian_rc=${lintian_rc}
EOF

cat >"$evidence/capacity.env" <<EOF
build_label=${label}
lto_mode=${lto_mode}
jobs=${jobs}
available_cpus=${available_cpus}
cgroup_cpu_quota_us=${cpu_quota_us}
cgroup_cpu_period_us=${cpu_period_us}
started_epoch=${start_epoch}
build_finished_epoch=${build_end_epoch}
attestation_finished_epoch=${attestation_end_epoch}
verification_finished_epoch=${verification_end_epoch}
build_elapsed_seconds=$((build_end_epoch - start_epoch))
attestation_elapsed_seconds=$((attestation_end_epoch - build_end_epoch))
lintian_elapsed_seconds=$((verification_end_epoch - attestation_end_epoch))
total_elapsed_seconds=$((verification_end_epoch - start_epoch))
root_used_bytes_before=${root_used_before}
peak_root_used_bytes=$(awk -F '\t' 'NR > 1 && $2 > max { max=$2 } END { print max+0 }' "$metrics")
peak_cgroup_memory_bytes=$(awk -F '\t' 'NR > 1 && $3 > max { max=$3 } END { print max+0 }' "$metrics")
cgroup_memory_peak_bytes=$(if [ -r /sys/fs/cgroup/memory.peak ]; then cat /sys/fs/cgroup/memory.peak; else awk -F '\t' 'NR > 1 && $3 > max { max=$3 } END { print max+0 }' "$metrics"; fi)
cgroup_memory_limit_bytes=$(if [ -r /sys/fs/cgroup/memory.max ]; then cat /sys/fs/cgroup/memory.max; else echo unknown; fi)
peak_cgroup_working_set_bytes=$(awk -F '\t' 'NR > 1 && $4 > max { max=$4 } END { print max+0 }' "$metrics")
peak_cgroup_anon_bytes=$(awk -F '\t' 'NR > 1 && $5 > max { max=$5 } END { print max+0 }' "$metrics")
peak_cgroup_file_bytes=$(awk -F '\t' 'NR > 1 && $6 > max { max=$6 } END { print max+0 }' "$metrics")
peak_cgroup_inactive_file_bytes=$(awk -F '\t' 'NR > 1 && $7 > max { max=$7 } END { print max+0 }' "$metrics")
peak_cgroup_active_file_bytes=$(awk -F '\t' 'NR > 1 && $8 > max { max=$8 } END { print max+0 }' "$metrics")
peak_cgroup_slab_reclaimable_bytes=$(awk -F '\t' 'NR > 1 && $9 > max { max=$9 } END { print max+0 }' "$metrics")
peak_cgroup_slab_unreclaimable_bytes=$(awk -F '\t' 'NR > 1 && $10 > max { max=$10 } END { print max+0 }' "$metrics")
cgroup_memory_pressure_some_delta_usec=$(awk -F '\t' 'NR == 2 { first=$11 } NR > 1 { last=$11 } END { delta=last-first; print delta < 0 ? 0 : delta }' "$metrics")
cgroup_memory_pressure_full_delta_usec=$(awk -F '\t' 'NR == 2 { first=$12 } NR > 1 { last=$12 } END { delta=last-first; print delta < 0 ? 0 : delta }' "$metrics")
cgroup_memory_high_bytes=$(if [ -r /sys/fs/cgroup/memory.high ]; then cat /sys/fs/cgroup/memory.high; else echo unknown; fi)
cgroup_memory_high_events=$(if [ -r /sys/fs/cgroup/memory.events ]; then awk '$1 == "high" { print $2 }' /sys/fs/cgroup/memory.events; else echo unknown; fi)
cgroup_memory_max_events=$(if [ -r /sys/fs/cgroup/memory.events ]; then awk '$1 == "max" { print $2 }' /sys/fs/cgroup/memory.events; else echo unknown; fi)
cgroup_oom_events=$(if [ -r /sys/fs/cgroup/memory.events ]; then awk '$1 == "oom" { print $2 }' /sys/fs/cgroup/memory.events; else echo unknown; fi)
cgroup_oom_kill_events=$(if [ -r /sys/fs/cgroup/memory.events ]; then awk '$1 == "oom_kill" { print $2 }' /sys/fs/cgroup/memory.events; else echo unknown; fi)
cgroup_swap_current_bytes=$(if [ -r /sys/fs/cgroup/memory.swap.current ]; then cat /sys/fs/cgroup/memory.swap.current; else echo unknown; fi)
cgroup_swap_limit_bytes=$(if [ -r /sys/fs/cgroup/memory.swap.max ]; then cat /sys/fs/cgroup/memory.swap.max; else echo unknown; fi)
root_sample_errors=${root_sample_errors}
artifact_bytes=$(du -sb "$artifacts" | awk '{print $1}')
EOF
(
	cd "$artifacts"
	sha256sum ./* | sort -k2
) >"$evidence/artifacts.sha256"
cp /work/inputs/source-inventory.json "$evidence/"
cp /work/inputs/toolchain.env "$evidence/"
cp /work/inputs/build-image-packages.tsv "$evidence/"
cp /work/inputs/apt-indexes.sha256 "$evidence/"
cp /work/inputs/build-image-debs.tsv "$evidence/"
cp /work/inputs/staging-apt-indexes.sha256 "$evidence/"
cp /work/inputs/repository-inputs.sha256 "$evidence/"
cp /work/inputs/publication-identity.json "$evidence/"
cp /work/inputs/policy-config-v2.json "$evidence/"
cp /work/inputs/policy-config-v3.json "$evidence/"
cp /work/inputs/policy-config-v4.json "$evidence/"
(
	cd "$evidence"
	sha256sum build.log >build.log.sha256
	xz --threads=1 --check=sha256 -1 build.log
)

if [ "$attestation_rc" -ne 0 ] || [ "$kbuild_audit_rc" -ne 0 ] ||
	[ "$simd_audit_rc" -ne 0 ] || [ "$lintian_rc" -ne 0 ]; then
	echo "[$label] post-build gates failed: package=${attestation_rc} Kbuild=${kbuild_audit_rc} SIMD=${simd_audit_rc} lintian=${lintian_rc}" >&2
	exit 1
fi

echo "[$label] build and non-boot verification PASS in $((verification_end_epoch - start_epoch))s" >&2
cd /work
rm -rf -- "$work" "$config_preflight"
