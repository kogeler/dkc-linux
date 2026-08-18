#!/usr/bin/env bash
# Snapshot the current published image bundle for one GitHub workflow.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd gh jq make sleep
dkc::install_cleanup_trap

[ "$#" -eq 2 ] || dkc::die \
	"usage: github-container-images-resolve.sh <event> <timeout>"
event="$1"
timeout="$2"
[ "${GITHUB_ACTIONS:-}" = true ] || dkc::die "this target requires GitHub Actions"
[ -n "${GITHUB_OUTPUT:-}" ] || dkc::die "GitHub output command file is absent"
[[ "$timeout" =~ ^[1-9][0-9]*$ ]] || dkc::die "image resolution timeout is invalid"

case "$event" in
pull_request) ;;
push | schedule | workflow_dispatch) ;;
*) dkc::die "unsupported GitHub image-consumer event" ;;
esac

started="$(date +%s)"
if [ "$event" != pull_request ]; then
	[ -n "${GH_TOKEN:-}" ] || dkc::die "GitHub Actions token is absent"
	[ -n "${GITHUB_REPOSITORY:-}" ] || dkc::die "GitHub repository identity is absent"
	# Workflow creation is asynchronous with respect to sibling workflows from
	# the same push. Give the image publisher a brief chance to become visible,
	# then wait for every active main publication before snapshotting latest.
	sleep 5
	while :; do
		active="$(gh api \
			"/repos/${GITHUB_REPOSITORY}/actions/workflows/container-images.yml/runs?branch=main&per_page=30" \
			--jq '[.workflow_runs[] | select(.status != "completed")] | length')"
		[[ "$active" =~ ^[0-9]+$ ]] || dkc::die "image workflow status is malformed"
		[ "$active" -gt 0 ] || break
		now="$(date +%s)"
		[ "$((now - started))" -lt "$timeout" ] ||
			dkc::die "active container image publication exceeded the resolution timeout"
		dkc::log "waiting for ${active} active container image publication run(s)"
		sleep 15
	done
fi

now="$(date +%s)"
remaining="$((timeout - (now - started)))"
[ "$remaining" -gt 0 ] || dkc::die "container image resolution timeout expired"
unset GH_TOKEN

resolved="$DKC_RUN_DIR/github-image-bundle.env"
dkc::register_resource path "$resolved"
make -C "$DKC_ROOT" container-images-resolve \
	DKC_IMAGE_RESOLVE_TIMEOUT="$remaining" \
	DKC_IMAGE_RESOLVE_OUTPUT="$resolved"
"$DKC_ROOT/scripts/github-ci.py" export-image-bundle --input "$resolved"
