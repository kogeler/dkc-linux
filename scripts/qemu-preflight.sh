#!/usr/bin/env bash
# Validate that KVM can instantiate the selected flavor CPU model.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd grep id qemu-system-x86_64 realpath sed timeout
dkc::install_cleanup_trap

[ "$#" -eq 2 ] || [ "$#" -eq 3 ] || dkc::die \
	"usage: qemu-preflight.sh <cpu-config> <kvm> [all|v2|v3|v4]"
config="$(realpath "$1")"
requested_accel="$2"
selection="${3:-all}"
case "$selection" in
all) selected='v2 v3 v4' ;;
v2 | v3 | v4) selected="$selection" ;;
*) dkc::die "preflight flavor must be all, v2, v3, or v4" ;;
esac
[ "$requested_accel" = kvm ] || dkc::die \
	"release VM qualification requires KVM; software emulation is not accepted"
case "$config" in
"${DKC_ROOT}/config/"*) ;;
*) dkc::die "CPU configuration must be inside config/" ;;
esac

# shellcheck disable=SC1090
source "$config"
: "${DKC_QEMU_MACHINE:?missing QEMU machine}"
: "${DKC_QEMU_CPU_V2:?missing v2 CPU model}"
: "${DKC_QEMU_CPU_V3:?missing v3 CPU model}"
: "${DKC_QEMU_CPU_V4:?missing v4 CPU model}"

for value in "$DKC_QEMU_MACHINE" "$DKC_QEMU_CPU_V2" "$DKC_QEMU_CPU_V3" "$DKC_QEMU_CPU_V4"; do
	[[ "$value" =~ ^[A-Za-z0-9_.=,+-]+$ ]] || dkc::die "unsafe QEMU model value: ${value}"
done
preflight_error=''
preflight_output=''
probe_models() {
	local candidate="$1" flavor model output rc
	for flavor in $selected; do
		case "$flavor" in
		v2) model="$DKC_QEMU_CPU_V2" ;;
		v3) model="$DKC_QEMU_CPU_V3" ;;
		v4) model="$DKC_QEMU_CPU_V4" ;;
		esac
		if output="$(timeout --signal=TERM --kill-after=1s 2s \
			qemu-system-x86_64 \
			-machine "${DKC_QEMU_MACHINE},accel=${candidate}" \
			-cpu "${model},enforce=on" -m 256 -nodefaults -display none -S 2>&1)"; then
			rc=0
		else
			rc=$?
		fi
		if [ "$rc" -ne 124 ]; then
			preflight_error="QEMU could not instantiate ${flavor} model ${model} with ${candidate} (rc=${rc})"
			preflight_output="$output"
			return 1
		fi
		if grep -Eqi "doesn't support requested feature|unable to find CPU model|invalid accelerator|property .* not found|failed to initialize" <<<"$output"; then
			preflight_error="QEMU cannot faithfully instantiate ${flavor} model ${model} with ${candidate}"
			preflight_output="$output"
			return 1
		fi
	done
	return 0
}

[ -e /dev/kvm ] || dkc::die "KVM device /dev/kvm is absent"
[ -c /dev/kvm ] || dkc::die "KVM path /dev/kvm is not a character device"
if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
	dkc::die "KVM device /dev/kvm is not readable and writable by $(id -un)"
fi
qemu-system-x86_64 -accel help 2>/dev/null | grep -qx kvm || dkc::die \
	"installed QEMU does not provide the KVM accelerator"
if ! probe_models kvm; then
	dkc::err "$preflight_error"
	if [ -n "$preflight_output" ]; then
		printf '%s\n' "$preflight_output" | sed 's/^/  qemu: /' >&2
	fi
	dkc::die "hardware-accelerated qualification cannot continue"
fi
accel=kvm

for flavor in $selected; do
	case "$flavor" in
	v2) model="$DKC_QEMU_CPU_V2" ;;
	v3) model="$DKC_QEMU_CPU_V3" ;;
	v4) model="$DKC_QEMU_CPU_V4" ;;
	esac
	dkc::log "QEMU ${flavor} model accepted with ${accel}: ${model}"
done

qemu_version="$(qemu-system-x86_64 --version | head -n 1)"
cat >"${DKC_RUN_DIR}/qemu-preflight.env" <<EOF
accelerator=${accel}
selection=${selection}
machine=${DKC_QEMU_MACHINE}
cpu_v2=${DKC_QEMU_CPU_V2}
cpu_v3=${DKC_QEMU_CPU_V3}
cpu_v4=${DKC_QEMU_CPU_V4}
EOF
dkc::ok "QEMU preflight PASS: accelerator=${accel}; ${qemu_version}"
