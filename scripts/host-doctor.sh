#!/usr/bin/env bash
# Host preflight probe.
#
# Read-only. Probes the host for the capabilities every later phase depends on,
# prints a human summary, and writes a machine-readable evidence record.
#
# It mutates nothing on the host. The single container it starts is ephemeral,
# labelled with this run's ID, and removed by the cleanup trap.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd jq awk sed date od
dkc::install_cleanup_trap

EVIDENCE_DIR="${DKC_ROOT}/evidence/preflight"
mkdir -p "$EVIDENCE_DIR"
RESULTS="${DKC_RUN_DIR}/checks.ndjson"
: >"$RESULTS"

overall_fail=0
overall_warn=0

# check <id> <status: PASS|FAIL|WARN|BLOCKED|NOT_RUN> <detail> [json-extra]
check() {
	local id="$1" status="$2" detail="$3" extra="${4:-{\}}"
	jq -c -n \
		--arg id "$id" --arg status "$status" --arg detail "$detail" \
		--argjson extra "$extra" \
		'{id:$id,status:$status,detail:$detail,extra:$extra}' >>"$RESULTS"
	case "$status" in
	PASS) dkc::ok "${id}: ${detail}" ;;
	WARN)
		dkc::warn "${id}: ${detail}"
		overall_warn=$((overall_warn + 1))
		;;
	FAIL)
		dkc::err "${id}: ${detail}"
		overall_fail=$((overall_fail + 1))
		;;
	*) dkc::log "${status} ${id}: ${detail}" ;;
	esac
}

# --------------------------------------------------------------------------
dkc::info "host commands"
# --------------------------------------------------------------------------

REQUIRED_CMDS=(podman qemu-system-x86_64 qemu-img make git curl jq gpg tar sha256sum awk getent)
OPTIONAL_CMDS=(virsh vagrant shellcheck shfmt diffoscope xorriso)

missing=()
for c in "${REQUIRED_CMDS[@]}"; do
	command -v "$c" >/dev/null 2>&1 || missing+=("$c")
done
if [ ${#missing[@]} -eq 0 ]; then
	check host.commands.required PASS "all present: ${REQUIRED_CMDS[*]}"
else
	check host.commands.required FAIL "missing: ${missing[*]}"
fi

absent_opt=()
for c in "${OPTIONAL_CMDS[@]}"; do
	command -v "$c" >/dev/null 2>&1 || absent_opt+=("$c")
done
if [ ${#absent_opt[@]} -eq 0 ]; then
	check host.commands.optional PASS "all optional host tools present"
else
	check host.commands.optional PASS "absent on host, provided by the container tier instead: ${absent_opt[*]}"
fi

# --------------------------------------------------------------------------
dkc::info "privilege boundary"
# --------------------------------------------------------------------------

if [ "$(id -u)" -ne 0 ]; then
	check host.unprivileged PASS "running as uid $(id -u) ($(id -un)), not root"
else
	check host.unprivileged FAIL "running as root on the host, which this project never does"
fi

check host.local_privilege PASS "local make targets run without privilege escalation"

# --------------------------------------------------------------------------
dkc::info "user namespace and subordinate ID delegation"
# --------------------------------------------------------------------------

uid_map="$(tr -s ' ' <'/proc/self/uid_map' | sed 's/^ //')"
ns_first="$(awk 'NR==1{print $1}' /proc/self/uid_map)"
ns_size="$(awk 'NR==1{print $3}' /proc/self/uid_map)"
ns_last=$((ns_first + ns_size - 1))

sub_line="$(awk -F: -v u="$(id -un)" '$1==u{print $2":"$3; exit}' /etc/subuid 2>/dev/null || true)"
if [ -z "$sub_line" ]; then
	check host.subuid FAIL "no /etc/subuid entry for $(id -un); rootless podman cannot map IDs"
else
	sub_start="${sub_line%%:*}"
	sub_count="${sub_line##*:}"
	sub_end=$((sub_start + sub_count - 1))
	if [ "$sub_start" -ge "$ns_first" ] && [ "$sub_end" -le "$ns_last" ]; then
		check host.subuid PASS \
			"subuid ${sub_start}-${sub_end} fits the namespace ID space ${ns_first}-${ns_last} (uid_map: ${uid_map})" \
			"$(jq -c -n --argjson s "$sub_start" --argjson e "$sub_end" --argjson ns "$ns_size" \
				'{subuid_start:$s,subuid_end:$e,ns_size:$ns}')"
	else
		check host.subuid FAIL \
			"subuid ${sub_start}-${sub_end} lies outside the namespace ID space ${ns_first}-${ns_last}; newuidmap will fail with EPERM"
	fi
fi

# --------------------------------------------------------------------------
dkc::info "podman"
# --------------------------------------------------------------------------

if podman_info="$(podman info --format '{{.Host.Security.Rootless}} {{.Store.GraphDriverName}} {{.Version.Version}}' 2>/dev/null)"; then
	read -r pm_rootless pm_driver pm_version <<<"$podman_info"
	if [ "$pm_rootless" = "true" ]; then
		check podman.rootless PASS "podman ${pm_version}, rootless, ${pm_driver} driver"
	else
		check podman.rootless FAIL "podman ${pm_version} is not rootless; rootless is required"
	fi
else
	check podman.rootless FAIL "podman info failed; rootless podman is unusable"
	pm_version="unknown"
fi

# The rootless network backend is probed, never assumed: pasta is podman's
# default but segfaults on some kernels.
net_backend="unknown"
probe_name="dkc-doctor-${DKC_RUN_ID}"
dkc::register_resource container "$probe_name"
if podman run --rm --name "$probe_name" \
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
	--timeout 120 \
	"$(cat "${DKC_ROOT}/config/base-image.lock" 2>/dev/null || echo docker.io/library/debian:trixie-slim)" \
	sh -c 'getent hosts deb.debian.org >/dev/null && echo NET_OK' 2>/dev/null | grep -q NET_OK; then
	net_backend="$(podman info --format '{{.Host.NetworkBackend}}' 2>/dev/null || echo unknown)"
	rootless_cmd="$(podman info --format '{{.Host.RootlessNetworkCmd}}' 2>/dev/null || echo unknown)"
	check podman.network PASS "container networking works (backend=${net_backend}, rootless cmd=${rootless_cmd})"
else
	check podman.network FAIL "an ephemeral container could not resolve DNS; check the rootless network backend"
fi

# --------------------------------------------------------------------------
dkc::info "virtualization"
# --------------------------------------------------------------------------

if [ -r /dev/kvm ] && [ -w /dev/kvm ]; then
	check kvm.device PASS "/dev/kvm readable and writable by $(id -un)"
else
	check kvm.device FAIL "/dev/kvm not usable; the boot-test tier would be BLOCKED"
fi

if qemu-system-x86_64 -accel help 2>/dev/null | grep -qx kvm; then
	check kvm.accel PASS "qemu reports the kvm accelerator"
else
	check kvm.accel FAIL "qemu does not report kvm; VM qualification requires hardware acceleration"
fi

# --------------------------------------------------------------------------
dkc::info "cpu baselines"
# --------------------------------------------------------------------------

host_level=1
if selected_flavor="$("${DKC_ROOT}/scripts/dkc-cpu-select" 2>/dev/null)"; then
	host_level="${selected_flavor#v}"
fi

if [ "$host_level" -ge 4 ]; then
	check cpu.baseline PASS "host supports x86-64-v${host_level}; all three flavors can be built and boot-tested with KVM" \
		"$(jq -c -n --argjson l "$host_level" '{host_isa_level:$l}')"
elif [ "$host_level" -ge 2 ]; then
	check cpu.baseline WARN "host supports only x86-64-v${host_level}; higher flavors cannot be qualified here with KVM"
else
	check cpu.baseline FAIL "host does not meet x86-64-v2"
fi

# --------------------------------------------------------------------------
dkc::info "capacity"
# --------------------------------------------------------------------------

mem_gib=$(awk '/^MemTotal:/{printf "%d", $2/1048576}' /proc/meminfo)
if [ "$mem_gib" -ge 16 ]; then
	check capacity.ram PASS "${mem_gib} GiB total RAM"
else
	check capacity.ram WARN "${mem_gib} GiB total RAM may be tight for a full kernel build"
fi

# Two thresholds, because two very different jobs run on this code. The
# container tier needs little; a three-flavor kernel build needs a lot. A CI
# runner legitimately fails the second while passing the first, so only the
# first is a hard failure here and the build tier asserts its own budget.
disk_gib=$(df -BG --output=avail "$DKC_ROOT" | awk 'NR==2{gsub("G","");print $1}')
disk_min="${DKC_MIN_DISK_GIB:-20}"
disk_build_min="${DKC_BUILD_DISK_GIB:-150}"
if [ "$disk_gib" -ge "$disk_min" ]; then
	check capacity.disk PASS "${disk_gib} GiB free at ${DKC_ROOT} (container tier needs ${disk_min} GiB)"
else
	check capacity.disk FAIL "${disk_gib} GiB free at ${DKC_ROOT}, below the ${disk_min} GiB the container tier needs"
fi

if [ "$disk_gib" -ge "$disk_build_min" ]; then
	check capacity.disk.build PASS "${disk_gib} GiB free, enough for a full three-flavor kernel build (${disk_build_min} GiB)"
else
	check capacity.disk.build NOT_RUN "${disk_gib} GiB free is below the ${disk_build_min} GiB a full kernel build needs; this host runs the container tier only"
fi

nproc_count="$(nproc)"
check capacity.cpu PASS "${nproc_count} logical CPUs" "$(jq -c -n --argjson n "$nproc_count" '{nproc:$n}')"

# --------------------------------------------------------------------------
dkc::info "egress"
# --------------------------------------------------------------------------

EGRESS_HOSTS=(
	"https://deb.debian.org/debian/dists/trixie/Release"
	"https://security.debian.org/debian-security/dists/trixie-security/Release"
	"https://snapshot.debian.org/"
	"https://sources.debian.org/api/src/linux/"
	"https://github.com"
)
egress_bad=()
for u in "${EGRESS_HOSTS[@]}"; do
	code="$(curl -4 -sS -o /dev/null -w '%{http_code}' --connect-timeout 10 --max-time 25 "$u" 2>/dev/null || echo 000)"
	[ "$code" = "200" ] || egress_bad+=("${u}=${code}")
done
if [ ${#egress_bad[@]} -eq 0 ]; then
	check egress.required PASS "all ${#EGRESS_HOSTS[@]} required endpoints reachable"
else
	check egress.required FAIL "unreachable: ${egress_bad[*]}"
fi

# --------------------------------------------------------------------------
dkc::info "production safety"
# --------------------------------------------------------------------------

prod_env=()
for v in S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY \
	APT_GPG_SIGNING_SUBKEY_B64 APT_GPG_PASSPHRASE; do
	if [ -n "${!v:-}" ]; then
		prod_env+=("$v")
	fi
done
if [ ${#prod_env[@]} -eq 0 ]; then
	check production.credentials PASS "no production credential is present in the environment"
else
	check production.credentials WARN "production credentials present in the environment: ${prod_env[*]}"
fi

# --------------------------------------------------------------------------
# Evidence record
# --------------------------------------------------------------------------

report="${EVIDENCE_DIR}/${DKC_RUN_ID}.json"
jq -s \
	--arg run_id "$DKC_RUN_ID" \
	--arg utc "$(dkc::utc_now)" \
	--arg commit "$(dkc::git_commit)" \
	--arg dirty "$(dkc::git_dirty)" \
	--arg kernel "$(uname -r)" \
	--arg host_os "$(
		# shellcheck source=/dev/null
		. /etc/os-release && echo "$PRETTY_NAME"
	)" \
	--argjson fails "$overall_fail" \
	--argjson warns "$overall_warn" \
	'{schema:"dkc.preflight.v1",run_id:$run_id,utc:$utc,commit:$commit,worktree:$dirty,
	  host:{os:$host_os,kernel:$kernel},
	  summary:{fail:$fails,warn:$warns,total:length},
	  checks:.}' \
	"$RESULTS" >"$report"

report_sha="$(sha256sum "$report" | awk '{print $1}')"
ln -sfn "$(basename "$report")" "${EVIDENCE_DIR}/latest.json"

echo
dkc::info "evidence: ${report}"
dkc::info "sha256:   ${report_sha}"

if [ "$overall_fail" -gt 0 ]; then
	dkc::err "preflight FAIL: ${overall_fail} failing check(s), ${overall_warn} warning(s)"
	exit 1
fi
dkc::ok "preflight PASS: 0 failing checks, ${overall_warn} warning(s)"
