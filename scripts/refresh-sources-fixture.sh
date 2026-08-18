#!/usr/bin/env bash
# Regenerate the `Sources` index fixture from the real Debian archive.
#
# The fixture exists because choosing the newest `src:linux` is the one decision
# that picks which kernel gets built, and the sid index carries many versions at
# once with the newest one neither first nor lexically largest. A synthetic
# fixture would not prove the parser survives the real thing.
#
# Networking is on for this target, which is why it is separate from the fast
# tier: fixtures are refreshed deliberately, and the tests that consume them run
# offline.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::install_cleanup_trap

PACKAGE="${1:-linux}"
SUITE="${2:-sid}"
OUT="${DKC_ROOT}/tests/fixtures/sources-${PACKAGE}-${SUITE}.txt"
captured="${DKC_RUN_DIR}/fixture.txt"

dkc::info "capturing ${PACKAGE} stanzas from ${SUITE}"

"${DKC_ROOT}/scripts/container-run.sh" --net --name fixture -- \
	scripts/in-container/sources-index.sh "$SUITE" "$PACKAGE" >"$captured"

count="$(grep -c "^Package: ${PACKAGE}\$" "$captured" || true)"
[ "${count:-0}" -gt 0 ] || dkc::die "captured no stanzas for ${PACKAGE}"

mkdir -p "$(dirname "$OUT")"
{
	printf '# Real %s stanzas from the Debian %s Sources index.\n' "$PACKAGE" "$SUITE"
	printf '# Captured %s by: make fixtures\n' "$(dkc::utc_now)"
	printf '# Only the parsed fields are kept; Binary and Build-Depends are omitted for size.\n'
	printf '# The commented release header records the authenticated metadata they came from.\n'
	cat "$captured"
} >"$OUT"

dkc::ok "wrote ${OUT}: ${count} stanzas, $(wc -c <"$OUT") bytes"
grep '^Version:' "$OUT" | sed 's/^/  /'
