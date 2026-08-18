#!/usr/bin/env bash
# Boot DKC kernels in direct QEMU guests and retain bounded guest evidence.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman qemu-system-x86_64 qemu-img timeout tar jq xz sha256sum sha512sum realpath
dkc::install_cleanup_trap

[ "$#" -eq 11 ] || dkc::die \
	"usage: qemu-boot.sh <toolbox> <image-config> <cpu-config> <flavor> <accel> <timeout-sec> <memory-mib> <cpus> <result> <kselftest-result> <update-latest>"
toolbox="$1"
image_config="$(realpath "$2")"
cpu_config="$(realpath "$3")"
flavor="$4"
requested_accel="$5"
timeout_seconds="$6"
memory_mib="$7"
vm_cpus="$8"
flavor_root="$9"
kselftest_root="${10}"
update_latest="${11}"

case "$flavor" in
v2 | v3 | v4) ;;
*) dkc::die "boot flavor must be v2, v3, or v4" ;;
esac
[[ "$update_latest" =~ ^[01]$ ]] || dkc::die "UPDATE_LATEST must be 0 or 1"
if [[ ! "$timeout_seconds" =~ ^[0-9]+$ ]] || [ "$timeout_seconds" -lt 600 ] || [ "$timeout_seconds" -gt 14400 ]; then
	dkc::die "QEMU timeout must be between 600 and 14400 seconds"
fi
if [[ ! "$memory_mib" =~ ^[0-9]+$ ]] || [ "$memory_mib" -lt 2048 ] || [ "$memory_mib" -gt 32768 ]; then
	dkc::die "QEMU memory must be between 2048 and 32768 MiB"
fi
if [[ ! "$vm_cpus" =~ ^[0-9]+$ ]] || [ "$vm_cpus" -lt 1 ] || [ "$vm_cpus" -gt 32 ]; then
	dkc::die "QEMU CPU count must be between 1 and 32"
fi

flavor_root="$(realpath "$flavor_root")"
kselftest_root="$(realpath "$kselftest_root")"
[ -d "$flavor_root/artifacts" ] || dkc::die "flavor result lacks artifacts: $flavor_root"
[ -d "$flavor_root/evidence" ] || dkc::die "flavor result lacks evidence: $flavor_root"
[ -d "$kselftest_root/evidence" ] || dkc::die "kselftest result lacks evidence: $kselftest_root"
lto_mode="$(jq -er \
	'.lto_mode | select(. == "none" or . == "thin" or . == "full")' \
	"$flavor_root/evidence/publication-identity.json")"
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] ||
	dkc::die "rootless podman is required"

"${DKC_ROOT}/scripts/qemu-preflight.sh" "$cpu_config" "$requested_accel" "$flavor"
accelerator="" machine="" cpu_v2="" cpu_v3="" cpu_v4=""
# shellcheck disable=SC1090
source "${DKC_RUN_DIR}/qemu-preflight.env"
case "$image_config" in
"${DKC_ROOT}/config/"*) ;;
*) dkc::die "image configuration must be inside config/" ;;
esac
# shellcheck disable=SC1090
source "$image_config"
: "${DKC_QEMU_IMAGE_URL:?missing image URL}"
: "${DKC_QEMU_IMAGE_FILENAME:?missing image filename}"
: "${DKC_QEMU_IMAGE_SHA512:?missing image SHA-512}"
[[ "$DKC_QEMU_IMAGE_FILENAME" =~ ^[A-Za-z0-9._-]+\.qcow2$ ]] ||
	dkc::die "unsafe QEMU image filename"
[[ "$DKC_QEMU_IMAGE_SHA512" =~ ^[0-9a-f]{128}$ ]] ||
	dkc::die "invalid QEMU image SHA-512"
base_image="${DKC_CACHE_DIR}/qemu/${DKC_QEMU_IMAGE_FILENAME}"
[ -f "$base_image" ] || dkc::die "QEMU base image is missing; run: make vm-base-image"
base_digest="$(sha512sum "$base_image" | awk '{print $1}')"
[ "$base_digest" = "$DKC_QEMU_IMAGE_SHA512" ] ||
	dkc::die "cached QEMU image checksum differs from the lock"
[ ! -w "$base_image" ] || dkc::die "QEMU base image must be read-only"

stage="${DKC_RUN_DIR}/qemu-boot"
mkdir -p "$stage"
dkc::register_resource path "$stage"
cat >"$stage/base-image.env" <<EOF
url=${DKC_QEMU_IMAGE_URL}
filename=${DKC_QEMU_IMAGE_FILENAME}
sha512=${base_digest}
EOF

output_root="$DKC_ROOT/out/qemu-boot"
output="$output_root/$DKC_RUN_ID"
exported=0

export_run() {
	local status="$1" scenario partial file
	[ "$exported" -eq 0 ] || return 0
	mkdir -p "$output_root"
	[ ! -e "$output" ] || dkc::die "refusing to replace existing QEMU output: $output"
	mkdir "$output"
	if [ -d "$stage/export" ]; then
		cp -a "$stage/export/." "$output/"
	fi
	mkdir -p "$output/evidence"
	if [ -d "$stage/evidence" ]; then
		cp -a "$stage/evidence/." "$output/evidence/"
	fi
	for file in base-image.env evidence-preparation.log; do
		[ -f "$stage/$file" ] && cp "$stage/$file" "$output/evidence/$file"
	done
	scenario="$stage/scenarios/$flavor"
	if [ -d "$scenario" ]; then
		partial="$output/$flavor/evidence"
		mkdir -p "$partial"
		for file in command-line.txt qemu-version.txt qemu.stderr qemu.stdout qemu-exit-code.txt input.env inputs.sha256; do
			if [ -f "$scenario/$file" ] && [ ! -e "$partial/$file" ]; then
				cp "$scenario/$file" "$partial/$file"
			fi
		done
		if [ -f "$scenario/serial.log" ] && [ ! -f "$partial/serial.log.xz" ]; then
			printf '%s  serial.log\n' "$(sha256sum "$scenario/serial.log" | awk '{print $1}')" \
				>"$partial/serial.log.sha256"
			xz --threads=1 --check=sha256 -1 -c "$scenario/serial.log" >"$partial/serial.log.xz"
		fi
	fi
	cat >"$output/evidence/result.env" <<EOF
status=${status}
flavor=${flavor}
lto_mode=${lto_mode}
accelerator=${accelerator}
machine=${machine}
publishable=false
EOF
	(
		cd "$output"
		find . -type f ! -path './evidence/evidence.sha256' -print0 |
			sort -z | xargs -0 -r sha256sum
	) >"$output/evidence/evidence.sha256"
	if [ "$status" = PASS ]; then
		if [ "$update_latest" = 1 ]; then
			ln -sfn "$DKC_RUN_ID" "$output_root/latest"
		fi
	else
		if [ "$update_latest" = 1 ]; then
			ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
		fi
	fi
	exported=1
}

on_error() {
	local rc=$?
	trap - ERR
	set +e
	export_run FAIL
	dkc::warn "retained QEMU failure evidence: $output"
	exit "$rc"
}
trap on_error ERR
on_signal() {
	local rc="$1"
	trap - ERR INT TERM
	set +e
	export_run FAIL
	dkc::warn "retained interrupted QEMU evidence: $output"
	exit "$rc"
}
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

prepare_name="dkc-qemu-inputs-${DKC_RUN_ID}"
dkc::register_resource container "$prepare_name"
dkc::info "preparing direct package and cloud-init inputs"
if dkc::archive_worktree |
	podman run --rm --interactive --network=none --read-only \
		--read-only-tmpfs=false --userns=keep-id \
		--name "$prepare_name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--pids-limit=4096 \
		--tmpfs=/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=1g,mode=1777 \
		--volume "${flavor_root}:/input/${flavor}:ro" \
		--volume "${kselftest_root}:/input/kselftest:ro" \
		--volume "$stage:/stage:rw" \
		--workdir /work --env HOME=/tmp/home \
		--env "DKC_RUN_ID=${DKC_RUN_ID}" \
		"$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		exec scripts/in-container/prepare-qemu-inputs.sh "$@"
	' sh /stage "$flavor" "/input/${flavor}" /input/kselftest \
		>"$stage/evidence-preparation.log" 2>&1; then
	:
else
	rc=$?
	tail -n 120 "$stage/evidence-preparation.log" >&2 || true
	dkc::die "QEMU input preparation failed with rc=${rc}"
fi

mkdir "$stage/export"

extract_results() {
	local scenario="$stage/scenarios/$flavor" extract_name
	mkdir -p "$scenario/guest"
	extract_name="dkc-qemu-results-${flavor}-${DKC_RUN_ID}"
	dkc::register_resource container "$extract_name"
	podman run --rm --network=none --read-only --read-only-tmpfs=false \
		--userns=keep-id --name "$extract_name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges --pids-limit=128 \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
		--volume "$scenario:/scenario:rw" "$toolbox" sh -ceu '
		e2fsck -fn /scenario/results.img
		debugfs -R "rdump / /scenario/guest" /scenario/results.img >/tmp/debugfs.log 2>&1
	' >/dev/null
}

run_flavor() {
	local scenario="$stage/scenarios/$flavor" overlay serial stderr stdout overlay_info
	local model qemu_rc=0 guest_status=FAIL export_dir flavor_status
	local -a qemu_args
	case "$flavor" in
	v2) model="$cpu_v2" ;;
	v3) model="$cpu_v3" ;;
	v4) model="$cpu_v4" ;;
	esac
	overlay="$scenario/root.qcow2"
	serial="$scenario/serial.log"
	stderr="$scenario/qemu.stderr"
	stdout="$scenario/qemu.stdout"
	qemu-img create -q -f qcow2 -F qcow2 -b "$base_image" "$overlay" 16G
	overlay_info="$(qemu-img info --output=json "$overlay")"
	[ "$(jq -r '.["backing-filename-format"]' <<<"$overlay_info")" = qcow2 ]
	[ "$(jq -r '.["full-backing-filename"]' <<<"$overlay_info")" = "$base_image" ]
	[ "$(jq -r '.["virtual-size"]' <<<"$overlay_info")" = 17179869184 ]

	qemu_args=(
		qemu-system-x86_64
		-name "dkc-${flavor}-${DKC_RUN_ID}"
		-machine "${machine},accel=${accelerator}"
		-cpu "${model},enforce=on"
		-smp "$vm_cpus"
		-m "$memory_mib"
		-nodefaults
		-device VGA
		-display none
		-monitor none
		-serial "file:${serial}"
		-rtc "base=utc,clock=host"
		-boot "order=c,strict=on"
		-drive "if=virtio,file=${overlay},format=qcow2,cache=none,discard=unmap"
		-drive "if=virtio,file=${scenario}/results.img,format=raw,cache=none"
		-drive "if=virtio,file=${scenario}/seed.iso,format=raw,readonly=on"
		-drive "if=virtio,file=${stage}/inputs.iso,format=raw,readonly=on"
		-netdev "user,id=net0"
		-device "virtio-net-pci,netdev=net0"
		-device virtio-rng-pci
	)
	printf '%q ' "${qemu_args[@]}" >"$scenario/command-line.txt"
	printf '\n' >>"$scenario/command-line.txt"
	qemu-system-x86_64 --version >"$scenario/qemu-version.txt"
	dkc::info "booting ${flavor}: accelerator=${accelerator} cpu=${model}"
	if timeout --signal=TERM --kill-after=30s "${timeout_seconds}s" \
		"${qemu_args[@]}" >"$stdout" 2>"$stderr"; then
		qemu_rc=0
	else
		qemu_rc=$?
	fi
	printf '%s\n' "$qemu_rc" >"$scenario/qemu-exit-code.txt"
	if ! extract_results; then
		dkc::warn "could not extract ${flavor} guest result disk"
		qemu_rc=1
	fi

	if [ -f "$scenario/guest/result.env" ]; then
		guest_status="$(awk -F= '$1 == "status" {print $2; exit}' "$scenario/guest/result.env")"
	fi
	if [ "$qemu_rc" -ne 0 ] || [ "$guest_status" != PASS ]; then
		dkc::warn "${flavor} boot validation failed: qemu_rc=${qemu_rc} guest=${guest_status}"
		flavor_status=FAIL
	else
		flavor_status=PASS
	fi
	if grep -Eqi "host doesn't support requested feature|failed to initialize" "$stderr"; then
		flavor_status=FAIL
		dkc::warn "${flavor} QEMU reported an unsupported requested feature"
	fi

	export_dir="$stage/export/$flavor"
	mkdir -p "$export_dir/guest" "$export_dir/evidence"
	for file in command-line.txt qemu-version.txt qemu.stderr qemu.stdout qemu-exit-code.txt input.env inputs.sha256; do
		[ -f "$scenario/$file" ] && cp "$scenario/$file" "$export_dir/evidence/$file"
	done
	if [ -d "$scenario/guest" ]; then
		cp -a "$scenario/guest/." "$export_dir/guest/"
	fi
	if [ -f "$serial" ]; then
		printf '%s  serial.log\n' "$(sha256sum "$serial" | awk '{print $1}')" \
			>"$export_dir/evidence/serial.log.sha256"
		xz --threads=1 --check=sha256 -1 -c "$serial" >"$export_dir/evidence/serial.log.xz"
	fi
	jq -n --arg status "$flavor_status" --arg flavor "$flavor" \
		--arg accelerator "$accelerator" --arg machine "$machine" --arg cpu "$model" \
		--argjson qemu_exit "$qemu_rc" \
		'{schema_version:1,status:$status,flavor:$flavor,accelerator:$accelerator,machine:$machine,cpu_model:$cpu,qemu_exit:$qemu_exit}' \
		>"$export_dir/evidence/result.json"
	(
		cd "$export_dir"
		find . -type f ! -path './evidence/evidence.sha256' -print0 |
			sort -z | xargs -0 -r sha256sum
	) >"$export_dir/evidence/evidence.sha256"
	[ "$flavor_status" = PASS ]
}

if run_flavor; then
	dkc::ok "${flavor} QEMU boot validation PASS"
else
	export_run FAIL
	dkc::die "QEMU boot validation failed for ${flavor}; evidence: ${output}"
fi

[ "$(sha512sum "$base_image" | awk '{print $1}')" = "$base_digest" ] ||
	dkc::die "immutable QEMU base image changed during validation"

export_run PASS
trap dkc::_on_err ERR
dkc::ok "QEMU boot validation complete: ${output}"
