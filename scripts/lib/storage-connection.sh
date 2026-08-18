#!/usr/bin/env bash
# Shared host-side staging for a private storage connection.

dkc::prepare_storage_connection() {
	[ "$#" -eq 3 ] || dkc::die \
		"usage: dkc::prepare_storage_connection <private-stage> <connection-or-empty> <output-variable>"
	local stage="$1" provided="$2" output_variable="$3"
	local -a arguments=(--output "$stage/connection.json")
	if [ -e "$stage" ] || [ -L "$stage" ]; then
		if [ -L "$stage" ] || [ ! -d "$stage" ]; then
			dkc::die "storage connection stage must be a real directory"
		fi
		[ "$(stat -c '%u' "$stage")" -eq "$(id -u)" ] || dkc::die \
			"storage connection stage must be owned by the current user"
		chmod 0700 "$stage"
	else
		mkdir -m 0700 "$stage"
	fi
	if [ -n "$provided" ]; then
		arguments+=(--provided "$provided")
	fi
	"$DKC_ROOT/scripts/storage-connection.py" "${arguments[@]}"
	unset S3_ENDPOINT S3_REGION S3_BUCKET S3_ADDRESSING_STYLE
	unset S3_ACCESS_KEY_ID S3_SECRET_ACCESS_KEY S3_SESSION_TOKEN
	printf -v "$output_variable" '%s' "$stage/connection.json"
}
