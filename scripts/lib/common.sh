#!/usr/bin/env bash
# DKC shared shell helpers.
#
# This file is sourced, never executed. It intentionally enables strict mode for
# the caller: every DKC script must fail closed.
#
# Contract notes:
#   - no command here requires root or sudo;
#   - no command here writes outside the declared paths;
#   - every ephemeral resource is registered so that cleanup can verify
#     ownership before removing anything.

if [ -n "${_DKC_COMMON_SOURCED:-}" ]; then
	return 0
fi
_DKC_COMMON_SOURCED=1

set -Eeuo pipefail

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

_dkc_detected_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
if [ -n "${DKC_ROOT:-}" ]; then
	_dkc_supplied_root="$(cd "$DKC_ROOT" 2>/dev/null && pwd -P)" || {
		printf 'FAIL invalid DKC_ROOT: %s\n' "$DKC_ROOT" >&2
		exit 1
	}
	if [ "$_dkc_supplied_root" != "$_dkc_detected_root" ]; then
		printf 'FAIL DKC_ROOT does not identify this script repository: %s != %s\n' \
			"$_dkc_supplied_root" "$_dkc_detected_root" >&2
		exit 1
	fi
fi
DKC_ROOT="$_dkc_detected_root"
export DKC_ROOT

DKC_CACHE_DIR="${DKC_CACHE_DIR:-${XDG_CACHE_HOME:-${HOME}/.cache}/dkc}"
export DKC_CACHE_DIR

DKC_RUN_ROOT="${DKC_ROOT}/.dkc-run"
export DKC_RUN_ROOT

# --------------------------------------------------------------------------
# Run identity
# --------------------------------------------------------------------------

# A run ID is unique per make invocation. Every container, network, volume, VM
# overlay, and scratch directory carries it so cleanup can never touch a
# resource this run did not create.
if [ -z "${DKC_RUN_ID:-}" ]; then
	DKC_RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"
fi
if [[ ! "$DKC_RUN_ID" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$ ]]; then
	printf 'FAIL unsafe DKC_RUN_ID: %s\n' "$DKC_RUN_ID" >&2
	exit 1
fi
export DKC_RUN_ID

DKC_RUN_DIR="${DKC_RUN_ROOT}/${DKC_RUN_ID}"
export DKC_RUN_DIR

# Ownership labels applied to every podman resource.
DKC_LABEL_NS="dkc.run-id"
DKC_LABEL_OWNER="dkc.owner"
DKC_OWNER_ID="dkc-$(printf '%s' "$DKC_ROOT" | sha256sum | awk '{print substr($1,1,16)}')"
export DKC_LABEL_NS DKC_LABEL_OWNER DKC_OWNER_ID

# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

if [ -t 2 ] && [ -z "${NO_COLOR:-}" ]; then
	_c_red=$'\033[31m'
	_c_yellow=$'\033[33m'
	_c_green=$'\033[32m'
	_c_blue=$'\033[34m'
	_c_dim=$'\033[2m'
	_c_off=$'\033[0m'
else
	_c_red='' _c_yellow='' _c_green='' _c_blue='' _c_dim='' _c_off=''
fi

dkc::_stamp() { date -u +%H:%M:%SZ; }

dkc::log() { printf '%s%s%s %s\n' "$_c_dim" "$(dkc::_stamp)" "$_c_off" "$*" >&2; }
dkc::info() { printf '%s%s%s %s==>%s %s\n' "$_c_dim" "$(dkc::_stamp)" "$_c_off" "$_c_blue" "$_c_off" "$*" >&2; }
dkc::ok() { printf '%s%s%s %sPASS%s %s\n' "$_c_dim" "$(dkc::_stamp)" "$_c_off" "$_c_green" "$_c_off" "$*" >&2; }
dkc::warn() { printf '%s%s%s %sWARN%s %s\n' "$_c_dim" "$(dkc::_stamp)" "$_c_off" "$_c_yellow" "$_c_off" "$*" >&2; }
dkc::err() { printf '%s%s%s %sFAIL%s %s\n' "$_c_dim" "$(dkc::_stamp)" "$_c_off" "$_c_red" "$_c_off" "$*" >&2; }

dkc::die() {
	dkc::err "$*"
	exit 1
}

# Report the failing command and line before exiting, so a partial run is never
# silently interpreted as a pass.
dkc::_on_err() {
	local rc=$? cmd=$BASH_COMMAND line=${BASH_LINENO[0]:-?} src=${BASH_SOURCE[1]:-?}
	dkc::err "aborted rc=${rc} at ${src}:${line}: ${cmd}"
	exit "$rc"
}
trap dkc::_on_err ERR

# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------

dkc::require_cmd() {
	local missing=()
	local c
	for c in "$@"; do
		command -v "$c" >/dev/null 2>&1 || missing+=("$c")
	done
	if [ ${#missing[@]} -gt 0 ]; then
		dkc::die "missing required host command(s): ${missing[*]}"
	fi
}

# Refuse to run as root: this project never escalates privilege on the host,
# and the build phase must be unprivileged.
dkc::refuse_root() {
	if [ "$(id -u)" -eq 0 ] && [ -z "${DKC_ALLOW_ROOT_IN_CONTAINER:-}" ]; then
		dkc::die "refusing to run as root on the host"
	fi
}

# --------------------------------------------------------------------------
# Scratch directories and cleanup
# --------------------------------------------------------------------------

# Resources created by this run, one "kind<TAB>id" record per line. Cleanup
# reads this file and re-verifies ownership labels before removing anything.
dkc::_resource_file() { printf '%s/resources.tsv' "$DKC_RUN_DIR"; }

dkc::run_dir_init() {
	local owner resource_file last_run
	mkdir -p "$DKC_RUN_ROOT"
	if [ -L "$DKC_RUN_ROOT" ] || [ ! -d "$DKC_RUN_ROOT" ]; then
		dkc::die "run scratch root must be a real directory"
	fi
	owner="$(stat -c '%u' "$DKC_RUN_ROOT")"
	[ "$owner" -eq "$(id -u)" ] || dkc::die \
		"run scratch root must be owned by the current user"
	chmod 0700 "$DKC_RUN_ROOT"
	if [ -e "$DKC_RUN_DIR" ] || [ -L "$DKC_RUN_DIR" ]; then
		if [ -L "$DKC_RUN_DIR" ] || [ ! -d "$DKC_RUN_DIR" ]; then
			dkc::die "run scratch path must be a real directory"
		fi
		[ "$(stat -c '%u' "$DKC_RUN_DIR")" -eq "$(id -u)" ] || dkc::die \
			"run scratch path must be owned by the current user"
		chmod 0700 "$DKC_RUN_DIR"
	else
		mkdir -m 0700 "$DKC_RUN_DIR"
	fi
	resource_file="$(dkc::_resource_file)"
	if [ -e "$resource_file" ] || [ -L "$resource_file" ]; then
		if [ -L "$resource_file" ] || [ ! -f "$resource_file" ]; then
			dkc::die "run resource journal must be a regular non-symlink file"
		fi
		[ "$(stat -c '%u' "$resource_file")" -eq "$(id -u)" ] || dkc::die \
			"run resource journal must be owned by the current user"
		chmod 0600 "$resource_file"
	else
		(umask 077 && : >"$resource_file")
	fi
	last_run="${DKC_RUN_ROOT}/.last-run-id"
	if [ -e "$last_run" ] || [ -L "$last_run" ]; then
		if [ -L "$last_run" ] || [ ! -f "$last_run" ]; then
			dkc::die "last-run marker must be a regular non-symlink file"
		fi
		[ "$(stat -c '%u' "$last_run")" -eq "$(id -u)" ] || dkc::die \
			"last-run marker must be owned by the current user"
		chmod 0600 "$last_run"
	fi
	(umask 077 && printf '%s\n' "$DKC_RUN_ID" >"$last_run")
}

dkc::register_resource() {
	local kind="${1:-}" id="${2:-}"
	if [ -z "$kind" ] || [ -z "$id" ]; then
		dkc::die "register_resource needs kind and id"
	fi
	case "${kind}${id}" in
	*$'\t'* | *$'\n'* | *$'\r'*) dkc::die "resource records must not contain control separators" ;;
	esac
	printf '%s\t%s\n' "$kind" "$id" >>"$(dkc::_resource_file)"
}

# Remove only resources this run created, and only after verifying the run-id
# label actually matches. Never a broad prune.
dkc::cleanup_run() {
	local rc=$?
	local file
	file="$(dkc::_resource_file)"
	[ -f "$file" ] || return $rc

	local kind id
	while IFS=$'\t' read -r kind id; do
		[ -n "${kind:-}" ] || continue
		case "$kind" in
		container)
			local label owner
			label="$(podman container inspect "$id" --format "{{index .Config.Labels \"${DKC_LABEL_NS}\"}}" 2>/dev/null || true)"
			owner="$(podman container inspect "$id" --format "{{index .Config.Labels \"${DKC_LABEL_OWNER}\"}}" 2>/dev/null || true)"
			if [ "$label" = "$DKC_RUN_ID" ] && [ "$owner" = "$DKC_OWNER_ID" ]; then
				podman rm -f -t 5 "$id" >/dev/null 2>&1 || dkc::warn "could not remove container ${id}"
			elif [ -n "$label" ] || [ -n "$owner" ]; then
				dkc::warn "refusing to remove container ${id}: ownership labels do not match this run"
			fi
			;;
		volume)
			local label owner
			label="$(podman volume inspect "$id" --format "{{index .Labels \"${DKC_LABEL_NS}\"}}" 2>/dev/null || true)"
			owner="$(podman volume inspect "$id" --format "{{index .Labels \"${DKC_LABEL_OWNER}\"}}" 2>/dev/null || true)"
			if [ "$label" = "$DKC_RUN_ID" ] && [ "$owner" = "$DKC_OWNER_ID" ]; then
				podman volume rm -f "$id" >/dev/null 2>&1 || dkc::warn "could not remove volume ${id}"
			elif [ -n "$label" ] || [ -n "$owner" ]; then
				dkc::warn "refusing to remove volume ${id}: label mismatch"
			fi
			;;
		pid)
			# Only signal a process this run started and that is still ours.
			if [ -n "$id" ] && kill -0 "$id" 2>/dev/null; then
				kill -TERM "$id" 2>/dev/null || true
			fi
			;;
		path)
			# Only paths strictly inside this run's scratch directory.
			case "$id" in
			"${DKC_RUN_DIR}"/*) rm -rf -- "$id" ;;
			*) dkc::warn "refusing to remove out-of-scope path ${id}" ;;
			esac
			;;
		*)
			dkc::warn "unknown resource kind ${kind}, not removing ${id}"
			;;
		esac
	done <"$file"

	return $rc
}

dkc::install_cleanup_trap() {
	dkc::run_dir_init
	trap 'dkc::cleanup_run' EXIT
	trap 'exit 130' INT
	trap 'exit 143' TERM
}

# Stream exactly the current project inputs: tracked files plus ordinary
# untracked files that are not excluded by the checkout. This preserves local
# edits without admitting build output, caches, or private material.
dkc::archive_worktree() {
	dkc::require_cmd git tar
	(
		cd "$DKC_ROOT"
		git ls-files --cached --others --exclude-standard -z |
			sort -z |
			while IFS= read -r -d '' path; do
				if [ -f "$path" ] || [ -L "$path" ]; then
					printf '%s\0' "$path"
				fi
			done |
			tar --create --file=- --null --verbatim-files-from --files-from=-
	)
}

# --------------------------------------------------------------------------
# Network fetch
# --------------------------------------------------------------------------

# Robust checksum-pinned downloader.
#
# Rationale: a mirror hostname can resolve to
# several addresses of which one stalls mid-TLS. curl picks one at random and
# does not fail over on a stall, so we iterate the resolved addresses
# explicitly with bounded timeouts, then verify the checksum. A download is
# never trusted without a checksum unless the caller explicitly passes "-".
dkc::fetch_digest() {
	local url="$1" dest="$2" algorithm="$3"
	local want_digest="${4:?checksum required, pass - to skip explicitly}"
	local attempts="${DKC_FETCH_ATTEMPTS:-3}"
	local connect_timeout="${DKC_FETCH_CONNECT_TIMEOUT:-10}"
	local max_time="${DKC_FETCH_MAX_TIME:-900}"
	local checksum_cmd
	case "$algorithm" in
	sha256 | sha512) checksum_cmd="${algorithm}sum" ;;
	*) dkc::die "unsupported checksum algorithm: ${algorithm}" ;;
	esac

	dkc::require_cmd curl getent "$checksum_cmd"

	local host port scheme
	scheme="${url%%://*}"
	host="${url#*://}"
	host="${host%%/*}"
	host="${host%%:*}"
	case "$scheme" in
	https) port=443 ;;
	http) port=80 ;;
	*) dkc::die "unsupported URL scheme in ${url}" ;;
	esac

	local -a addrs=()
	mapfile -t addrs < <(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u)
	if [ ${#addrs[@]} -eq 0 ]; then
		dkc::warn "no IPv4 address for ${host}, letting curl resolve"
		addrs=("")
	fi

	mkdir -p "$(dirname "$dest")"
	local tmp="${dest}.part.${DKC_RUN_ID}"

	local attempt addr
	for ((attempt = 1; attempt <= attempts; attempt++)); do
		for addr in "${addrs[@]}"; do
			local -a resolve=()
			[ -n "$addr" ] && resolve=(--resolve "${host}:${port}:${addr}")
			dkc::log "fetch attempt ${attempt} ${url}${addr:+ via ${addr}}"
			if curl -fsSL --ipv4 \
				--connect-timeout "$connect_timeout" \
				--max-time "$max_time" \
				"${resolve[@]}" \
				-o "$tmp" "$url"; then
				if [ "$want_digest" = "-" ]; then
					mv -f "$tmp" "$dest"
					return 0
				fi
				local got
				got="$("$checksum_cmd" "$tmp" | awk '{print $1}')"
				if [ "$got" = "$want_digest" ]; then
					mv -f "$tmp" "$dest"
					dkc::log "fetch ok ${algorithm}=${got}"
					return 0
				fi
				rm -f "$tmp"
				dkc::die "checksum mismatch for ${url}: want ${want_digest} got ${got}"
			fi
			rm -f "$tmp"
		done
		sleep "$((attempt * 2))"
	done
	dkc::die "failed to fetch ${url} after ${attempts} attempts over ${#addrs[@]} address(es)"
}

# --------------------------------------------------------------------------
# Misc
# --------------------------------------------------------------------------

dkc::utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

dkc::git_commit() { git -C "$DKC_ROOT" rev-parse HEAD 2>/dev/null || printf 'none'; }

dkc::git_dirty() {
	if [ -n "$(git -C "$DKC_ROOT" status --porcelain 2>/dev/null)" ]; then
		printf 'dirty'
	else
		printf 'clean'
	fi
}
