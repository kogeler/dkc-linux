#!/usr/bin/env bash
# Verify the locked Debian source, DKC overlay, toolchain, and dependency closure.
#
# Runs INSIDE the build container. The source members are fetched and extracted
# once through their verified .dsc. The control file is regenerated with
# Debian's own generator, never hand-edited, so what is checked here is what a
# real build would see.
#
# The assertions are the ones from the specification's toolchain gate: no
# fabricated `-for-host` package, no GNU-triplet-prefixed Clang, no leftover
# reference to the Sid-only compiler, and the LLVM packages must be separate
# dependencies rather than alternatives.
#
# Needs network to fetch the source. Not part of the offline fast tier.

set -Eeuo pipefail

DSC_URL="${1:?dsc url required}"
DSC_SHA256="${2:?dsc sha256 required}"
DSC_SIZE="${3:?dsc size required}"
ORIG_TAR_URL="${4:?orig tar url required}"
ORIG_TAR_SHA256="${5:?orig tar sha256 required}"
ORIG_TAR_SIZE="${6:?orig tar size required}"
DEBIAN_TAR_URL="${7:?debian tar url required}"
DEBIAN_TAR_SHA256="${8:?debian tar sha256 required}"
DEBIAN_TAR_SIZE="${9:?debian tar size required}"
LLVM_MAJOR="${10:?llvm major required}"

# The work area must be under /work, not /tmp: the container mounts /tmp
# noexec, and Debian's control generator executes helper scripts from the
# unpacked source tree.
work="$(mktemp -d /work/overlay-XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
cd "$work"

fetch() {
	local url="$1" expected_sha="$2" expected_size="$3" output="$4"
	PYTHONPATH=/work/src python3 -m dkc.cli.download \
		--user-agent dkc-release-preflight/1 "$url" "$output"
	printf '%s  %s\n' "$expected_sha" "$output" | sha256sum -c - >/dev/null
	actual_size="$(stat -c %s "$output")"
	[ "$actual_size" = "$expected_size" ] || {
		echo "source size mismatch for ${output}: ${actual_size} != ${expected_size}" >&2
		exit 1
	}
}

dsc="$(basename "$DSC_URL")"
orig="$(basename "$ORIG_TAR_URL")"
debian="$(basename "$DEBIAN_TAR_URL")"
fetch "$DSC_URL" "$DSC_SHA256" "$DSC_SIZE" "$dsc"
fetch "$ORIG_TAR_URL" "$ORIG_TAR_SHA256" "$ORIG_TAR_SIZE" "$orig"
fetch "$DEBIAN_TAR_URL" "$DEBIAN_TAR_SHA256" "$DEBIAN_TAR_SIZE" "$debian"
dpkg-source -x "$dsc" source >/dev/null
cd source

# The reviewed overlay, build profiles and audit policy belong to the source
# profile of this exact Debian source, never to whichever series is newest.
source_version="$(dpkg-parsechangelog -SVersion)"
PROFILE_DIR="$(PYTHONPATH=/work/src python3 -m dkc.sourceprofile \
	/work/src "$source_version" directory)"
PATCH_DIR="$(PYTHONPATH=/work/src python3 -m dkc.sourceprofile \
	/work/src "$source_version" overlay-directory)"
DKC_BUILD_PROFILES="$(PYTHONPATH=/work/src python3 -m dkc.sourceprofile \
	/work/src "$source_version" build-profiles)"
export DEB_BUILD_PROFILES="${DKC_BUILD_PROFILES}"
# Debian's own name for the compiler this source selects. The overlay replaces
# it with LLVM, so no generated relation may still reference it afterwards.
stock_compiler="$(python3 -c \
	'import pathlib,sys,tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["build"]["c_compiler"])' \
	debian/config/defines.toml)"
[[ "$stock_compiler" =~ ^gcc-[0-9]+$ ]] || {
	echo "unexpected Debian compiler selection: ${stock_compiler}" >&2
	exit 1
}
echo "source profile: ${PROFILE_DIR#/work/src/}, Debian compiler: ${stock_compiler}"

fail=0
note() { printf '  %-46s %s\n' "$1" "$2"; }
assert_zero() {
	local label="$1" count="$2"
	if [ "$count" -eq 0 ]; then
		note "$label" "ok (0)"
	else
		note "$label" "FAIL (${count})"
		fail=$((fail + 1))
	fi
}
assert_nonzero() {
	local label="$1" count="$2"
	if [ "$count" -gt 0 ]; then
		note "$label" "ok (${count})"
	else
		note "$label" "FAIL (0)"
		fail=$((fail + 1))
	fi
}

echo "=== stock source, before the overlay ==="
python3 debian/bin/gencontrol.py >/dev/null 2>&1
note "${stock_compiler}-for-host in generated control" "$(grep -c 'gcc-[0-9]*-for-host' debian/control || true)"

echo
echo "=== applying the overlay ==="
# Anchored edits, checked against the committed patches: context that moved
# since the reviewed source is tolerated, a different changed line is not.
PYTHONPATH=/work/src python3 -m dkc.cli.generate_overlay_patches \
	--apply . "$LLVM_MAJOR" "$PATCH_DIR"

echo
echo "=== regenerating with Debian's own generator ==="
python3 debian/bin/gencontrol.py >/dev/null 2>&1
# Render the auxiliary .install/.links inputs as the real build does.  Running
# gencontrol.py alone proves control relations but leaves those package files
# absent, which would make a header-path assertion test only its template.
make -B -f debian/rules debian/control-real >/dev/null

assert_zero "no fabricated -for-host dependency" \
	"$(grep -c -- '-for-host' debian/control || true)"
assert_zero "no GNU-triplet-prefixed clang" \
	"$(grep -c 'linux-gnu-clang' debian/control || true)"
assert_zero "no stale Sid-only gcc dependency" \
	"$(grep -c "$stock_compiler" debian/control || true)"
assert_nonzero "headers depend on the real clang package" \
	"$(grep -c "clang-${LLVM_MAJOR}" debian/control || true)"

# The three LLVM packages must be separate dependencies. As alternatives, apt
# would satisfy the group with clang alone and the build would have no linker.
compiler_line="$(grep -m1 '^Build-Depends-Arch:' debian/control || true)"
for tool in clang lld llvm; do
	if printf '%s' "$compiler_line" | grep -q "${tool}-${LLVM_MAJOR} "; then
		note "${tool}-${LLVM_MAJOR} present in Build-Depends-Arch" "ok"
	else
		note "${tool}-${LLVM_MAJOR} present in Build-Depends-Arch" "FAIL"
		fail=$((fail + 1))
	fi
done
if printf '%s' "$compiler_line" | grep -qE "clang-${LLVM_MAJOR}[^,]*\| *lld-${LLVM_MAJOR}"; then
	note "LLVM packages are separate, not alternatives" "FAIL"
	fail=$((fail + 1))
else
	note "LLVM packages are separate, not alternatives" "ok"
fi

# The installed headers invoke all three packages directly. clang-N does not
# install ld.lld-N or the full versioned llvm-* tool set, so checking clang alone
# would produce a package that installs successfully and then fails every plain
# external-module build.
selected_packages="$(dh_listpackages)"
architecture_packages="$(dh_listpackages -a)"
nokbuild_architecture_packages="$(
	DEB_BUILD_PROFILES="${DKC_BUILD_PROFILES} pkg.dkc.nokbuild" dh_listpackages -a
)"
export DKC_SELECTED_PACKAGES="$selected_packages"
PYTHONPATH=/work/src python3 -m dkc.cli.verify_overlay package-graph "$LLVM_MAJOR"

# The per-flavor builder relies on dpkg's binary target split: v2 emits the
# one Architecture:all common package together with Architecture:any output,
# while v3/v4 emit only Architecture:any and suppress the unique Kbuild
# package. Prove those selection primitives against the generated control file
# before a compiler is started.
independent_only="$(
	comm -23 \
		<(printf '%s\n' "$selected_packages" | sort) \
		<(printf '%s\n' "$architecture_packages" | sort)
)"
profile_excluded="$(
	comm -23 \
		<(printf '%s\n' "$architecture_packages" | sort) \
		<(printf '%s\n' "$nokbuild_architecture_packages" | sort)
)"
if [ "$(wc -l <<<"$architecture_packages")" -eq 25 ] &&
	[ "$(wc -l <<<"$nokbuild_architecture_packages")" -eq 24 ] &&
	[[ "$independent_only" =~ ^dkc-linux-headers-.+-common$ ]] &&
	[[ "$profile_excluded" =~ ^dkc-linux-kbuild-.+$ ]]; then
	note "binary target/profile split is exact" "ok (26/25/24)"
else
	note "binary target/profile split is exact" "FAIL"
	printf '  all=%s arch=%s profiled-arch=%s independent=%q excluded=%q\n' \
		"$(wc -l <<<"$selected_packages")" \
		"$(wc -l <<<"$architecture_packages")" \
		"$(wc -l <<<"$nokbuild_architecture_packages")" \
		"$independent_only" "$profile_excluded"
	fail=$((fail + 1))
fi

if grep -Ev '^dkc-linux-' <<<"$selected_packages" | grep -q .; then
	note "selected binaries stay in dkc-linux namespace" "FAIL"
	fail=$((fail + 1))
else
	note "selected binaries stay in dkc-linux namespace" "ok"
fi
if [ "$(wc -l <<<"$selected_packages")" -eq 26 ]; then
	note "three-flavor package graph is exact" "ok (26)"
else
	note "three-flavor package graph is exact" "FAIL:$(wc -l <<<"$selected_packages")"
	fail=$((fail + 1))
fi
for forbidden in linux-libc-dev linux-bpf-dev linux-perf linux-cpupower; do
	if grep -qx "$forbidden" <<<"$selected_packages"; then
		note "unpublished ${forbidden} excluded" "FAIL"
		fail=$((fail + 1))
	else
		note "unpublished ${forbidden} excluded" "ok"
	fi
done

# The versioned Kbuild package executes dh_python3 in binary_kbuild.  DKC keeps
# that package while deliberately applying Debian's broad pkg.linux.notools
# profile, so the helper dependency must follow the narrower DKC profile too.
if awk '
	/^Build-Depends:/ { in_build_depends=1 }
	in_build_depends && /dh-python <!pkg[.]dkc[.]nokbuild>/ { found=1 }
	in_build_depends && /^[^[:space:]][^:]*:/ && !/^Build-Depends:/ { in_build_depends=0 }
	END { exit !found }
' debian/control; then
	note "dh-python follows the selected Kbuild profile" "ok"
else
	note "dh-python follows the selected Kbuild profile" "FAIL"
	fail=$((fail + 1))
fi
if grep -q '^dkc-linux-kbuild-' <<<"$selected_packages"; then
	if command -v dh_python3 >/dev/null; then
		note "selected Kbuild helper dh_python3 is installed" "ok"
	else
		note "selected Kbuild helper dh_python3 is installed" "FAIL"
		fail=$((fail + 1))
	fi
fi

if grep -qF "\${python3:Depends}" debian/control; then
	note "selected package metadata has no stale substitutions" "FAIL"
	fail=$((fail + 1))
else
	note "selected package metadata has no stale substitutions" "ok"
fi
if [ ! -s debian/templates/base.meta.lintian-overrides.j2 ] &&
	[ ! -s debian/templates/image.meta.lintian-overrides.j2 ] &&
	! grep -q 'usr-share-doc-symlink-to-foreign-package' \
		debian/templates/image.lintian-overrides.j2; then
	note "obsolete documentation overrides are absent" "ok"
else
	note "obsolete documentation overrides are absent" "FAIL"
	fail=$((fail + 1))
fi
# Debian renames these templates between packaging generations; check whichever
# spelling this source ships.
mapfile -t meta_templates < <(
	find debian/templates -maxdepth 1 -type f \
		\( -name 'image.meta.control.*' -o -name 'headers.meta.control.*' \
		-o -name 'image-dbg.meta.control.*' \) | sort
)
if [ "${#meta_templates[@]}" -ne 3 ]; then
	note "package synopses use current metapackage spelling" "FAIL: templates"
	fail=$((fail + 1))
elif grep -R -q 'meta-package' "${meta_templates[@]}"; then
	note "package synopses use current metapackage spelling" "FAIL"
	fail=$((fail + 1))
else
	note "package synopses use current metapackage spelling" "ok"
fi

# Renaming a binary package must not accidentally rename the Linux headers
# filesystem ABI.  Debian derives BASE_DIR and several link targets from the
# package name; without these explicit overrides the common `source` symlink is
# dangling and conventional /usr/src/linux-headers-<KREL> discovery breaks.
PYTHONPATH=/work/src python3 -m dkc.cli.verify_overlay header-paths

# Lifecycle scripts are part of the package contract, not incidental debhelper
# output.  Assert the pinned Debian source still generates the hooks that the
# clean package clients rely on; the matrix later rechecks the rendered .deb
# control archives with their exact KRELs.
PYTHONPATH=/work/src python3 -m dkc.cli.verify_overlay lifecycle-scripts

# Debian 13 clients have linux-base 4.12, whose bootloader and initramfs hooks
# read the kernel, its configuration and its System.map from /boot. Newer
# Debian generations install them below the modules directory and rely on
# linux-base 4.17 hooks that Debian 13 does not have.
DEBIAN_13_LINUX_BASE=4.12.1
python3 - "$DEBIAN_13_LINUX_BASE" <<'PAYLOAD'
import os
import pathlib
import re
import subprocess
import sys

floor = sys.argv[1]
# Other Debian architectures keep their own flavours in this unrestricted tree;
# only the selected amd64 product packages are the DKC payload contract.
selected = set(os.environ["DKC_SELECTED_PACKAGES"].splitlines())
payloads = {
    "dkc-linux-binary-": ("boot/vmlinu*-*",),
    "dkc-linux-base-": ("boot/config-*", "boot/System.map-*"),
}
for prefix, expected in payloads.items():
    installs = sorted(
        path
        for path in pathlib.Path("debian").glob(f"{prefix}*.install.amd64")
        if path.name.removesuffix(".install.amd64") in selected
    )
    if len(installs) != 3:
        raise SystemExit(
            f"expected three selected {prefix}*.install.amd64 files, found {len(installs)}"
        )
    for path in installs:
        lines = path.read_text(encoding="utf-8").split()
        for payload in expected:
            if payload not in lines:
                raise SystemExit(f"{path.name} does not ship {payload} for Debian 13")

control = pathlib.Path("debian/control").read_text(encoding="utf-8")
required = set()
for paragraph in control.split("\n\n"):
    package = re.search(r"(?m)^Package: (dkc-linux-image-[0-9][^\n]*)$", paragraph)
    pre_depends = re.search(r"(?m)^Pre-Depends: ([^\n]*)$", paragraph)
    if package is None:
        continue
    if pre_depends is None:
        raise SystemExit(f"{package.group(1)} lost its linux-base Pre-Depends")
    relation = re.search(r"linux-base \(>= ([^)]+)\)", pre_depends.group(1))
    if relation is None:
        raise SystemExit(f"{package.group(1)} does not pre-depend on linux-base")
    required.add(relation.group(1))
if len(required) != 1:
    raise SystemExit(f"image packages require inconsistent linux-base versions: {sorted(required)}")
demanded = required.pop()
if subprocess.run(
    ["dpkg", "--compare-versions", floor, "ge", demanded], check=False
).returncode:
    raise SystemExit(
        f"image packages require linux-base {demanded}, which Debian 13 ({floor}) cannot satisfy"
    )
print(f"  kernel payload stays in /boot and linux-base (>= {demanded}) is satisfiable on Debian 13: ok")
PAYLOAD

echo
echo "=== the toolchain actually reaches Kbuild ==="

# Dependencies alone prove nothing: a build can resolve clang and still invoke
# gcc. These assert the selection reaches the compiler.
# shellcheck disable=SC2016  # searching for a literal make expression
if grep -q 'LLVM=-$(LLVM_MAJOR)' debian/rules.real; then
	note "LLVM=-N passed on the Kbuild command line" "ok"
else
	note "LLVM=-N passed on the Kbuild command line" "FAIL"
	fail=$((fail + 1))
fi

# .kernelvariables is included after the kernel has already bound CC, LD and the
# rest to GNU names, so LLVM= alone there would be ignored. Every tool must be
# named. This file also ships in the headers package and drives DKMS.
missing_tools=""
for tool in CC LD AR NM OBJCOPY OBJDUMP READELF STRIP HOSTCC; do
	grep -q "echo '${tool} = .*\$(LLVM_MAJOR)'" debian/rules.real ||
		missing_tools="${missing_tools} ${tool}"
done
if [ -z "$missing_tools" ]; then
	note "every tool named in .kernelvariables" "ok"
else
	note "every tool named in .kernelvariables" "FAIL:${missing_tools}"
	fail=$((fail + 1))
fi

if grep -q "CROSS_COMPILE)clang" debian/rules.real; then
	note "no GNU triplet prepended to clang" "FAIL"
	fail=$((fail + 1))
else
	note "no GNU triplet prepended to clang" "ok"
fi

echo
echo "=== unsigned initial product and deterministic module policy ==="
if grep -q '^enable_signed = false$' debian/config/amd64/defines.toml; then
	note "amd64 official signing stage disabled" "ok"
else
	note "amd64 official signing stage disabled" "FAIL"
	fail=$((fail + 1))
fi
if grep -q '^# CONFIG_MODULE_SIG is not set$' debian/config/config; then
	note "random module signing disabled in config" "ok"
else
	note "random module signing disabled in config" "FAIL"
	fail=$((fail + 1))
fi
if grep -q -- '-o MODULE_SIG=n' debian/rules.real; then
	note "module-signing disablement has override priority" "ok"
else
	note "module-signing disablement has override priority" "FAIL"
	fail=$((fail + 1))
fi
if grep -q '^# CONFIG_SECURITY_LOCKDOWN_LSM is not set$' debian/config/config &&
	grep -q '^# CONFIG_LOCK_DOWN_IN_EFI_SECURE_BOOT is not set$' debian/config/config; then
	note "unsigned product does not claim EFI lockdown" "ok"
else
	note "unsigned product does not claim EFI lockdown" "FAIL"
	fail=$((fail + 1))
fi
for forbidden in KBUILD_SIGN_PIN signing_key.pem; do
	if grep -q "$forbidden" debian/rules.real; then
		note "no ${forbidden} in packaging rules" "FAIL"
		fail=$((fail + 1))
	else
		note "no ${forbidden} in packaging rules" "ok"
	fi
done
for tool in strip objcopy; do
	if grep -q "llvm-${tool}-\$(LLVM_MAJOR)" debian/rules.real; then
		note "packaging ${tool} uses selected LLVM" "ok"
	else
		note "packaging ${tool} uses selected LLVM" "FAIL"
		fail=$((fail + 1))
	fi
done
if dh_listpackages | grep -q -- '-dbg$'; then
	note "debug package excluded by the active profile" "FAIL"
	fail=$((fail + 1))
else
	note "debug package excluded by the active profile" "ok"
fi
if dh_listpackages | grep -q -- '-di$'; then
	note "installer udebs excluded by both noudeb profiles" "FAIL"
	fail=$((fail + 1))
else
	note "installer udebs excluded by both noudeb profiles" "ok"
fi
# shellcheck disable=SC2016  # literal Make expressions are the search patterns
if grep -q '^ifeq (,$(filter pkg.linux.nokerneldbg,$(DEB_BUILD_PROFILES)))$' debian/rules.real &&
	grep -q 'install -D -m644 $(DIR)/System.map $(OUTPUT_DIR)/boot/System.map-' debian/rules.real; then
	note "unused debug staging skipped; real System.map retained" "ok"
else
	note "unused debug staging skipped; real System.map retained" "FAIL"
	fail=$((fail + 1))
fi

echo
echo "=== x86-64 flavor policy and compiler semantics ==="
PYTHONPATH=/work/src python3 -m dkc.cli.verify_overlay flavor-policy

no_simd=(-mno-sse -mno-mmx -mno-sse2 -mno-3dnow -mno-avx -mno-sse4a)
rust_no_simd='-Ctarget-feature=-sse,-sse2,-sse3,-ssse3,-sse4.1,-sse4.2,-avx,-avx2'
printf '%s\n' \
	'void add(int *d, const int *a, const int *b) {' \
	'  for (int i = 0; i < 256; ++i) d[i] = a[i] + b[i];' \
	'}' >"$work/vector-probe.c"

check_minimum() {
	local tool="$1" actual="$2" minimum
	minimum="$(scripts/min-tool-version.sh "$tool")"
	if dpkg --compare-versions "$actual" ge "$minimum"; then
		note "${tool} ${actual} meets upstream minimum" "ok (${minimum})"
	else
		note "${tool} ${actual} meets upstream minimum" "FAIL:${minimum}"
		fail=$((fail + 1))
	fi
}
check_minimum rustc "$(rustc --version | sed -n 's/^rustc \([^[:space:]]*\).*/\1/p')"
check_minimum bindgen "$(bindgen --version | sed -n 's/^bindgen \([^[:space:]]*\).*/\1/p')"
check_minimum llvm "$("clang-${LLVM_MAJOR}" --version | sed -n '1s/.*version \([0-9][^[:space:]]*\).*/\1/p')"
for flavor in v2 v3 v4; do
	config_dir="$work/config-${flavor}"
	oldconfig_dir="$work/config-${flavor}-oldconfig"
	mkdir -p "$config_dir"
	mkdir -p "$oldconfig_dir"
	debian/bin/kconfig.py "$config_dir/.config" \
		debian/config/config debian/config/amd64/config \
		"debian/config/amd64/config.${flavor}-amd64" \
		-o "BUILD_SALT=\"overlay-${flavor}\"" -o MODULE_SIG=n
	cp "$config_dir/.config" "$oldconfig_dir/.config"
	make LLVM="-${LLVM_MAJOR}" ARCH=x86 O="$config_dir" olddefconfig >/dev/null
	make LLVM="-${LLVM_MAJOR}" ARCH=x86 O="$oldconfig_dir" listnewconfig >/dev/null
	(
		set +o pipefail
		yes "" | make LLVM="-${LLVM_MAJOR}" ARCH=x86 O="$oldconfig_dir" oldconfig >/dev/null
	)
	if cmp -s "$config_dir/.config" "$oldconfig_dir/.config"; then
		note "${flavor} preflight and Debian setup configs are byte-identical" "ok"
	else
		note "${flavor} preflight and Debian setup configs are byte-identical" "FAIL"
		fail=$((fail + 1))
	fi
	if grep -qx 'CONFIG_RUST=y' "$config_dir/.config"; then
		note "${flavor} keeps Rust support enabled" "ok"
	else
		note "${flavor} keeps Rust support enabled" "FAIL"
		fail=$((fail + 1))
	fi
	if grep -qx "CONFIG_DKC_X86_64_BASELINE_${flavor^^}=y" "$config_dir/.config" &&
		[ "$(grep -Ec '^CONFIG_DKC_X86_64_BASELINE_(V2|V3|V4)=y$' "$config_dir/.config")" -eq 1 ]; then
		note "${flavor} selects exactly one Kconfig baseline" "ok"
	else
		note "${flavor} selects exactly one Kconfig baseline" "FAIL"
		fail=$((fail + 1))
	fi
	# Query a target that includes the configured architecture Makefile but does
	# not start the default build (plain `make -pn` tries to build objtool and
	# returns 2 when this deliberately minimal config tree has no tools/ output).
	resolved_target="$(make LLVM="-${LLVM_MAJOR}" ARCH=x86 O="$config_dir" -pn image_name 2>/dev/null |
		awk '/^DKC_X86_64_TARGET := / {print $3; found=1} END {if (!found) exit 1}')"
	if [ "$resolved_target" = "x86-64-${flavor}" ]; then
		note "${flavor} resolves C/Rust target CPU" "ok"
	else
		note "${flavor} resolves C/Rust target CPU" "FAIL:${resolved_target}"
		fail=$((fail + 1))
	fi
	clang="clang-${LLVM_MAJOR}"
	objdump="llvm-objdump-${LLVM_MAJOR}"
	"$clang" -target x86_64-linux-gnu -O3 -ffreestanding \
		-march="x86-64-${flavor}" "${no_simd[@]}" \
		-c "$work/vector-probe.c" -o "$work/vector-probe-${flavor}.o"
	if "$objdump" --no-show-raw-insn --disassemble "$work/vector-probe-${flavor}.o" |
		grep -Eq '(%(xmm|ymm|zmm|mm|k)[0-9]+|[[:space:]](emms|vzeroall|vzeroupper)[[:space:]])'; then
		note "${flavor} Clang no-SIMD codegen probe" "FAIL"
		fail=$((fail + 1))
	else
		note "${flavor} Clang no-SIMD codegen probe" "ok"
	fi
	if ! rust_cfg="$(rustc --target=x86_64-unknown-none \
		-Ctarget-cpu="x86-64-${flavor}" "$rust_no_simd" --print cfg 2>/dev/null)"; then
		note "${flavor} Rust no-SIMD cfg probe" "FAIL: rustc"
		fail=$((fail + 1))
	elif grep -Eq 'target_feature="(sse|sse2|sse3|ssse3|sse4\.1|sse4\.2|avx|avx2|avx512.*)"' <<<"$rust_cfg"; then
		note "${flavor} Rust no-SIMD cfg probe" "FAIL"
		fail=$((fail + 1))
	else
		note "${flavor} Rust no-SIMD cfg probe" "ok"
	fi
done

# shellcheck disable=SC2016  # literal Make expressions are the search patterns
if [ "$(grep -c '^        KBUILD_CFLAGS += $(X86_CFLAGS_NO_SIMD)$' arch/x86/Makefile)" -ne 1 ] ||
	[ "$(grep -c '^KBUILD_CFLAGS += $(X86_CFLAGS_NO_SIMD)$' arch/x86/Makefile)" -ne 1 ]; then
	note "no-SIMD policy occurs before and after baseline" "FAIL"
	fail=$((fail + 1))
else
	note "no-SIMD policy occurs before and after baseline" "ok"
fi

echo
echo "=== exact-source kselftest profile ==="
# The selftest bundle is built hours later, from an accepted flavor. Its exact
# selection and configuration requirements can be checked against this source
# and the configurations above right now, so an upstream rename or a disabled
# requirement fails here instead of after a complete kernel build.
PYTHONPATH=/work/src python3 - "$PROFILE_DIR" "$work" <<'KSELFTEST'
import pathlib
import re
import sys

from dkc.sourceprofile import load_profile

profile_dir, work = (pathlib.Path(value) for value in sys.argv[1:3])
selftest = load_profile(profile_dir).kselftest
root = pathlib.Path("tools/testing/selftests")
wrappers = pathlib.Path("/work/src/tests/integration/kselftest-wrappers")

absent = [target for target in selftest.targets if not (root / target).is_dir()]
if absent:
    raise SystemExit(f"selected kselftest collections are absent from this source: {absent}")

declared: dict[str, set[str]] = {}
for collection in selftest.targets:
    directory = root / collection
    # A collection names its tests in the Makefile, or builds them from a
    # wildcard over its sources, so both spellings count as declared.
    names = {path.name for path in directory.iterdir()}
    names |= {path.stem for path in directory.iterdir()}
    makefile = directory / "Makefile"
    if makefile.is_file():
        names |= set(re.findall(r"[A-Za-z0-9_./-]+", makefile.read_text(encoding="utf-8")))
    declared[collection] = names

unknown = []
for selector in selftest.tests:
    collection, _, test = selector.partition(":")
    # DKC installs its own reviewed wrapper for a selector named after it.
    if (wrappers / f"{collection.replace('/', '-')}-{test}").is_file():
        continue
    # Upstream builds several x86 tests in both bit widths from one source.
    base = re.sub(r"_(?:32|64)$", "", test)
    if {test, base} & declared[collection]:
        continue
    unknown.append(selector)
if unknown:
    raise SystemExit(f"selected kselftests are not declared by this source: {unknown}")

for flavor in ("v2", "v3", "v4"):
    configuration = (work / f"config-{flavor}/.config").read_text(encoding="utf-8").splitlines()
    values = dict(
        line.split("=", 1) for line in configuration if line.startswith("CONFIG_") and "=" in line
    )
    for symbol in selftest.required_builtin:
        if values.get(f"CONFIG_{symbol}") != "y":
            raise SystemExit(f"{flavor} does not build CONFIG_{symbol} in, as the profile requires")
    for symbol in selftest.required_enabled:
        if values.get(f"CONFIG_{symbol}") not in ("y", "m"):
            raise SystemExit(f"{flavor} does not enable CONFIG_{symbol}, as the profile requires")

print(
    f"  {len(selftest.targets)} collections, {len(selftest.tests)} selected tests and every "
    "Kconfig requirement exist in this source: ok"
)
KSELFTEST

echo
echo "=== amd64 headers package ==="
awk '/^Package: dkc-linux-headers-.*-amd64$/,/^$/' debian/control | grep -m1 '^Depends:' | fold -w 100 | sed 's/^/  /'

echo
echo "=== dependency resolution on Trixie ==="
if dpkg-checkbuilddeps debian/control 2>&1; then
	note "dpkg-checkbuilddeps" "ok, all satisfied"
else
	note "dpkg-checkbuilddeps" "FAIL, unmet dependencies above"
	fail=$((fail + 1))
fi

PYTHONPATH=/work/src python3 -m dkc.cli.verify_overlay build-dependencies "$PWD/debian/control" "$DKC_BUILD_PROFILES"

source_version="$(dpkg-parsechangelog -SVersion)"
PYTHONPATH=/work/src python3 -m dkc.cli.verify_overlay prepend-test-changelog "$source_version"
if python3 debian/bin/gencontrol.py >/dev/null 2>&1 &&
	[ "$(dpkg-parsechangelog -SDistribution)" = trixie ]; then
	note "derived package version is valid for Trixie" "ok"
else
	note "derived package version is valid for Trixie" "FAIL"
	fail=$((fail + 1))
fi

echo
if [ "$fail" -gt 0 ]; then
	echo "RESULT: FAIL, ${fail} assertion(s) failed" >&2
	exit 1
fi
echo "RESULT: PASS, the overlay closes the toolchain gap and owns the DKC flavor/package namespace"
