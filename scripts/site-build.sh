#!/usr/bin/env bash
# Build and audit the public documentation site in a confined offline container.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd git podman sha256sum tar
dkc::install_cleanup_trap

[ "$#" -eq 3 ] || dkc::die "usage: site-build.sh <toolbox> <source-revision> <source-epoch>"
toolbox="$1"
revision="$2"
source_epoch="$3"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]] || dkc::die "site source revision is not a full Git SHA"
[[ "$source_epoch" =~ ^[1-9][0-9]*$ ]] || dkc::die "site source epoch must be positive"
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"

# The sitemap dates each page by its source's last commit. A shallow clone
# would date every page by the checked-out commit instead.
[ "$(git -C "$DKC_ROOT" rev-parse --is-shallow-repository)" = false ] ||
	dkc::die "the site build needs complete Git history; fetch with depth 0"
mkdir -p "$DKC_RUN_DIR"
page_dates="$DKC_RUN_DIR/site-page-dates.tsv"
dkc::register_resource path "$page_dates"
(
	cd "$DKC_ROOT"
	git ls-files -z -- '*.md' |
		while IFS= read -r -d '' path; do
			epoch="$(git log -1 --format=%ct -- "$path")"
			[ -z "$epoch" ] || printf '%s\t%s\n' "$epoch" "$path"
		done
) >"$page_dates"

output_root="$DKC_ROOT/out/site"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace site build output"
name="dkc-site-build-${DKC_RUN_ID}"
dkc::register_resource container "$name"
if ! dkc::archive_worktree |
	podman run --rm --interactive --network=none --read-only --read-only-tmpfs=false \
		--userns=keep-id --name "$name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--no-hosts --ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=256 --umask=077 --log-driver=none \
		--env HOME=/tmp/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777 \
		--tmpfs=/work:rw,noexec,nosuid,nodev,size=512m,mode=1777 \
		--volume "$output_root:/output:rw" \
		--volume "$page_dates:/input/page-dates.tsv:ro" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo /work/build "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		export PYTHONPATH=/work/repo
		exec python3 -m dkc.cli.site_build \
			--repository /work/repo --workspace /work/build \
			--output "/output/$1" --revision "$2" --source-epoch "$3" \
			--page-dates /input/page-dates.tsv
	' sh "$DKC_RUN_ID" "$revision" "$source_epoch"; then
	dkc::die "site build failed"
fi
(
	cd "$output"
	sha256sum --check evidence.sha256 >/dev/null
)
ln -sfn "$DKC_RUN_ID" "$output_root/latest"
dkc::ok "site build passed: out/site/${DKC_RUN_ID}/site"
