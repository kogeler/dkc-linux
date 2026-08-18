#!/usr/bin/env bash
# Run a command inside a confined, ephemeral, rootless container.
#
# General toolbox checks go through here so isolation, ownership labelling, and
# cleanup are implemented once. The same invocation works on a GitHub-hosted
# ubuntu-26.04 runner, which ships the same Podman 5.7.0 as this host, so CI
# exercises the real flow instead of a workflow-only reimplementation.
#
# The repository is NOT bind-mounted. Sources are streamed in as a tar on stdin
# and unpacked into a work directory inside the container; results come back on
# stdout. A container that cannot prove its own confinement does no work.
#
# Usage:
#   scripts/container-run.sh [options] -- <command> [args...]
#
# Options:
#   --image REF      image to run (default: the DKC toolbox image)
#   --profile P      hermetic (default) | debug
#   --net            allow networking (default: none)
#   --name NAME      container name suffix, for logs and cleanup
#
# Profiles:
#   hermetic  tmpfs work area, no mounts, no network unless --net. Default.
#   debug     repository bind-mounted read-only for an interactive shell, since
#             a tar on stdin cannot coexist with a terminal. No test uses it.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman tar
dkc::install_cleanup_trap

IMAGE="${DKC_TOOLBOX_IMAGE:-localhost/dkc-toolbox:latest}"
PROFILE="hermetic"
WITH_NET=0
NAME_SUFFIX="run"

while [ $# -gt 0 ]; do
	case "$1" in
	--image)
		IMAGE="$2"
		shift 2
		;;
	--profile)
		PROFILE="$2"
		shift 2
		;;
	--net)
		WITH_NET=1
		shift
		;;
	--name)
		NAME_SUFFIX="$2"
		shift 2
		;;
	--)
		shift
		break
		;;
	*) dkc::die "unknown option: $1" ;;
	esac
done

[ $# -gt 0 ] || dkc::die "no command given; use -- <command>"

case "$NAME_SUFFIX" in
*[!a-zA-Z0-9_.-]* | "") dkc::die "unsafe container name suffix: ${NAME_SUFFIX}" ;;
esac

case "$PROFILE" in
hermetic | debug) ;;
*) dkc::die "unknown profile ${PROFILE}; expected hermetic or debug" ;;
esac

# Rootless is a requirement, not a preference: a rootful container would run the
# build as real root on the host.
rootless="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo false)"
[ "$rootless" = "true" ] || dkc::die "rootless podman is required, got rootless=${rootless}"

name="dkc-${NAME_SUFFIX}-${DKC_RUN_ID}"
dkc::register_resource container "$name"

# --------------------------------------------------------------------------
# Confinement
# --------------------------------------------------------------------------

# shellcheck disable=SC2054  # the commas belong inside single --tmpfs values,
# they are not array element separators.
flags=(
	--rm
	--name "$name"
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}"
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}"
	--pull=missing
	--read-only
	--read-only-tmpfs=false
	--userns=keep-id
	--cap-drop=ALL
	--security-opt=no-new-privileges
	--no-hosts
	--ipc=private
	--pid=private
	--uts=private
	--cgroupns=private
	--pids-limit=1024
	--umask=077
	--log-driver=none
	--env HOME=/tmp/home
	--env LC_ALL=C.UTF-8
	--env LANG=C.UTF-8
	--env TZ=UTC
	--env "DKC_RUN_ID=${DKC_RUN_ID}"
	--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777
)

if [ "$WITH_NET" -eq 1 ]; then
	# Use Podman's configured default unless the caller explicitly selects a
	# compatible network backend for this invocation.
	if [ -n "${DKC_PODMAN_NETWORK:-}" ]; then
		flags+=(--network="$DKC_PODMAN_NETWORK")
	fi
else
	flags+=(--network=none)
fi

case "$PROFILE" in
hermetic)
	# mode=1777 because the tmpfs is created owned by the userns root, not by
	# the keep-id user; the prologue then creates /work/src as 0700 and owns it.
	flags+=("--tmpfs=/work:rw,exec,nosuid,nodev,size=2g,mode=1777")
	;;
debug)
	dkc::warn "debug profile: the repository is bind-mounted read-only; no test target uses this"
	flags+=(--volume "${DKC_ROOT}:/work/src:ro" --interactive --tty)
	;;
esac

# --------------------------------------------------------------------------
# Prologue
# --------------------------------------------------------------------------

# The container proves its confinement before touching anything: not uid 0, no
# effective capabilities, no-new-privileges set. All prologue output goes to
# stderr so the command owns stdout, which is how results stream back out.
# shellcheck disable=SC2016  # deliberately unexpanded: this is evaluated by the
# shell inside the container, not by this one.
ASSERT_CONFINED='
	test "$(id -u)" -ne 0 || { echo "running as root inside the container" >&2; exit 1; }
	grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status || { echo "effective capabilities are not empty" >&2; exit 1; }
	grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status || { echo "NoNewPrivs is not set" >&2; exit 1; }
	mkdir -p "$HOME" && chmod 700 "$HOME"
'

if [ "$PROFILE" = "debug" ]; then
	exec podman run "${flags[@]}" "$IMAGE" sh -ceu "${ASSERT_CONFINED}"'
		cd /work/src
		exec "$@"
	' sh "$@"
fi

# Stream the current project inputs into the isolated work area.
dkc::archive_worktree |
	podman run --interactive "${flags[@]}" "$IMAGE" sh -ceu "${ASSERT_CONFINED}"'
		test ! -e /work/src || { echo "unexpected pre-existing /work/src" >&2; exit 1; }
		mkdir -p /work/src && chmod 700 /work/src
		tar --extract --file=- --directory=/work/src
		cd /work/src
		exec "$@"
	' sh "$@"
