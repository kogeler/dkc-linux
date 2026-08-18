#!/usr/bin/env bash
# Refuse a stale signing run by resolving canonical main without credentials.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd git
[ "$#" -eq 2 ] || dkc::die "usage: check-current-main.sh <owner/repository> <expected-sha>"
repository="$1"
expected="$2"
[[ "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
	dkc::die "unsafe canonical repository name"
[[ "$expected" =~ ^[0-9a-f]{40}$ ]] || dkc::die "expected commit is not a full Git SHA"
mapfile -t records < <(
	git ls-remote --exit-code "https://github.com/${repository}.git" refs/heads/main
)
[ "${#records[@]}" -eq 1 ] || dkc::die "canonical main did not resolve to exactly one ref"
read -r current ref <<<"${records[0]}"
if [ "$ref" != refs/heads/main ] || [[ ! "$current" =~ ^[0-9a-f]{40}$ ]]; then
	dkc::die "canonical main returned a malformed ref"
fi
[ "$current" = "$expected" ] ||
	dkc::die "workflow commit is stale: canonical main has advanced"
dkc::ok "workflow commit is current canonical main"
