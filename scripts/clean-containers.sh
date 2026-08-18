#!/usr/bin/env bash
# Remove only containers owned by this exact DKC working tree.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman

mapfile -t ids < <(
	podman ps -aq --filter "label=${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" 2>/dev/null
)
if [ ${#ids[@]} -eq 0 ]; then
	echo "no containers owned by ${DKC_OWNER_ID}"
	exit 0
fi

for id in "${ids[@]}"; do
	owner="$(podman container inspect "$id" --format "{{index .Config.Labels \"${DKC_LABEL_OWNER}\"}}")"
	run_id="$(podman container inspect "$id" --format "{{index .Config.Labels \"${DKC_LABEL_NS}\"}}")"
	if [ "$owner" != "$DKC_OWNER_ID" ]; then
		dkc::warn "refusing to remove ${id}: owner label changed"
		continue
	fi
	if [[ ! "$run_id" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$ ]]; then
		dkc::warn "refusing to remove ${id}: malformed run-id label"
		continue
	fi
	echo "removing ${id} (run ${run_id})"
	podman rm -f -t 5 "$id" >/dev/null
done
