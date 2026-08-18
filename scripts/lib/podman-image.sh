# Shared normalization for Podman's local image config identity.

dkc::canonical_podman_image_id() {
	[ "$#" -eq 1 ] || return 2
	local value="$1"
	if [[ "$value" =~ ^[0-9a-f]{64}$ ]]; then
		printf 'sha256:%s\n' "$value"
	elif [[ "$value" =~ ^sha256:[0-9a-f]{64}$ ]]; then
		printf '%s\n' "$value"
	else
		return 1
	fi
}
