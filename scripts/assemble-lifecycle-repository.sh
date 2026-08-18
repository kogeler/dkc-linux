#!/usr/bin/env bash
# Assemble a bootstrap, update, or maintenance repository from typed lifecycle state.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root

[ "$#" -eq 11 ] || dkc::die \
	"usage: assemble-lifecycle-repository.sh <toolbox> <matrix-or-empty> <keys> <epoch> <generation> <state-present> <previous-pool> <previous-state> <retention-mode> <retention-max-bytes-or-empty> <mode>"
toolbox="$1"
matrix="$2"
keys="$3"
epoch="$4"
generation="$5"
state_present="$6"
previous_pool="$7"
previous_state="$8"
retention_mode="$9"
retention_max_bytes="${10}"
mode="${11}"

case "$state_present" in
true)
	if [ ! -d "$previous_pool" ] || [ ! -d "$previous_state" ]; then
		dkc::die "present lifecycle state requires its authenticated pool and state handoffs"
	fi
	;;
false)
	[ "$mode" = build ] || dkc::die "metadata maintenance requires prior state"
	previous_pool=""
	previous_state=""
	;;
*) dkc::die "authoritative state presence must be exactly true or false" ;;
esac
case "$mode" in
build) [ -d "$matrix" ] || dkc::die "build assembly requires a package matrix" ;;
maintenance)
	[ "$state_present" = true ] || dkc::die "maintenance requires prior state"
	[ -z "$matrix" ] || dkc::die "maintenance must not receive a package matrix"
	;;
*) dkc::die "lifecycle assembly mode must be build or maintenance" ;;
esac

exec "$DKC_ROOT/scripts/assemble-apt-repository.sh" \
	"$toolbox" "$matrix" "$keys" "$epoch" "$generation" \
	"$previous_pool" "$previous_state" "$retention_mode" "$retention_max_bytes"
