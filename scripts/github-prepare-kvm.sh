#!/usr/bin/env bash
# Install QEMU and require faithful KVM execution on a hosted GitHub runner.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd apt-get chmod env grep stat sudo

[ "$#" -eq 2 ] || dkc::die \
	"usage: github-prepare-kvm.sh <cpu-config> <v2|v3|v4>"
cpu_config="$1"
flavor="$2"
[ "${GITHUB_ACTIONS:-}" = true ] || dkc::die "this target requires GitHub Actions"
case "$flavor" in v2 | v3 | v4) ;; *) dkc::die "unsupported KVM flavor" ;; esac

sudo apt-get update
sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 \
	apt-get install -y --no-install-recommends qemu-system-x86 qemu-utils

[ -e /dev/kvm ] || dkc::die "KVM device /dev/kvm is absent on the hosted runner"
[ -c /dev/kvm ] || dkc::die "KVM path /dev/kvm is not a character device"
stat -c 'kvm before: mode=%a owner=%U group=%G type=%F' /dev/kvm
sudo chmod 0666 /dev/kvm || dkc::die "unable to make /dev/kvm accessible"
stat -c 'kvm after: mode=%a owner=%U group=%G type=%F' /dev/kvm
if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
	dkc::die "KVM device is not readable and writable by the runner user"
fi

"$DKC_ROOT/scripts/qemu-preflight.sh" "$cpu_config" kvm "$flavor"
dkc::ok "hosted KVM is ready for ${flavor}"
