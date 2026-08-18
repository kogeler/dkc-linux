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
PATCH_DIR="${11:-/work/src/debian-overlay/patches}"

# shellcheck disable=SC1091  # provided by the repository
. /work/src/config/build-profiles
export DEB_BUILD_PROFILES="${DKC_BUILD_PROFILES}"

# The work area must be under /work, not /tmp: the container mounts /tmp
# noexec, and Debian's control generator executes helper scripts from the
# unpacked source tree.
work="$(mktemp -d /work/overlay-XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
cd "$work"

fetch() {
	local url="$1" expected_sha="$2" expected_size="$3" output="$4"
	python3 - "$url" "$output" <<'PY'
import pathlib
import sys
import time
import urllib.request

url, output = sys.argv[1:]
target = pathlib.Path(output)
partial = target.with_suffix(target.suffix + ".partial")
for attempt in range(1, 5):
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "dkc-release-preflight/1"}
        )
        with urllib.request.urlopen(request, timeout=180) as response, partial.open(
            "wb"
        ) as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        partial.replace(target)
        break
    except Exception as error:
        partial.unlink(missing_ok=True)
        if attempt == 4:
            raise
        print(
            f"download attempt {attempt}/4 failed for {url}: {error}; retrying",
            file=sys.stderr,
            flush=True,
        )
        time.sleep(attempt)
PY
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
note "gcc-15-for-host in generated control" "$(grep -c 'gcc-[0-9]*-for-host' debian/control || true)"

echo
echo "=== applying the overlay ==="
for patch in "$PATCH_DIR"/*.patch; do
	echo "  $(basename "$patch")"
	patch -p1 --batch --forward --silent <"$patch"
done

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
	"$(grep -c 'gcc-15' debian/control || true)"
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
python3 - "$LLVM_MAJOR" <<'PY'
import os
import re
import sys

major = sys.argv[1]
selected = set(os.environ["DKC_SELECTED_PACKAGES"].splitlines())
paragraphs = open("debian/control", encoding="utf-8").read().split("\n\n")
headers = []
header_metas = []
images = []
packages = {}
for paragraph in paragraphs:
    fields = {}
    current = None
    for line in paragraph.splitlines():
        if line[:1] in (" ", "\t") and current:
            fields[current] += " " + line.strip()
            continue
        key, sep, value = line.partition(":")
        if sep:
            current = key
            fields[key] = value.strip()
    package = fields.get("Package", "")
    if package in selected:
        if package in packages:
            raise SystemExit(f"duplicate generated binary package: {package}")
        packages[package] = fields
    if re.fullmatch(r"dkc-linux-headers-[0-9].*-amd64", package):
        headers.append((package, fields.get("Depends", "")))
    elif re.fullmatch(r"dkc-linux-headers-v[234]-amd64", package):
        header_metas.append((package, fields.get("Depends", "")))
    if re.fullmatch(r"dkc-linux-(?:image|modules|binary).*amd64", package):
        images.append((package, fields.get("Depends", "")))

if not headers:
    raise SystemExit("no amd64 headers packages were generated")
for package, depends in headers:
    missing = [
        tool
        for tool in ("clang", "lld", "llvm")
        if not re.search(rf"(?:^|, ){tool}-{re.escape(major)}(?:[, (]|$)", depends)
    ]
    if missing:
        raise SystemExit(f"{package} lacks client tool dependencies: {missing}")

# A headers meta-package need not duplicate those dependencies, but it must
# depend on the corresponding versioned headers package so the tools remain a
# mandatory transitive dependency rather than a Suggests/Recommends accident.
for package, depends in header_metas:
    suffix = package.removeprefix("dkc-linux-headers-")
    if not re.search(
        rf"(?:^|, )dkc-linux-headers-[0-9][^, ]*-{re.escape(suffix)}(?:[, (]|$)",
        depends,
    ):
        raise SystemExit(f"{package} does not require its versioned headers package")

for package, depends in images:
    leaked = [
        tool
        for tool in ("clang", "lld", "llvm")
        if re.search(rf"(?:^|, ){tool}-{re.escape(major)}(?:[, (]|$)", depends)
    ]
    if leaked:
        raise SystemExit(f"{package} unexpectedly depends on compilers: {leaked}")

common_names = [name for name in packages if re.fullmatch(r"dkc-linux-headers-.+-common", name)]
kbuild_names = [name for name in packages if re.fullmatch(r"dkc-linux-kbuild-.+", name)]
if len(common_names) != 1 or len(kbuild_names) != 1:
    raise SystemExit("generated common headers/Kbuild package is not unique")
common, kbuild = common_names[0], kbuild_names[0]
abi = common.removeprefix("dkc-linux-headers-").removesuffix("-common")
if kbuild != f"dkc-linux-kbuild-{abi}":
    raise SystemExit("common headers and Kbuild ABI identities differ")

release_by_flavor = {}
for name in packages:
    if match := re.fullmatch(r"dkc-linux-image-([0-9].+-(v[234])-amd64)", name):
        release_by_flavor[match.group(2)] = match.group(1)
if set(release_by_flavor) != {"v2", "v3", "v4"}:
    raise SystemExit(f"cannot derive exact generated flavor releases: {release_by_flavor}")

expected_graph = {common: {}, kbuild: {}}
expected_provides = {}
for flavor, release in release_by_flavor.items():
    base = f"dkc-linux-base-{release}"
    binary = f"dkc-linux-binary-{release}"
    modules = f"dkc-linux-modules-{release}"
    image = f"dkc-linux-image-{release}"
    versioned_headers = f"dkc-linux-headers-{release}"
    base_meta = f"dkc-linux-base-{flavor}-amd64"
    image_meta = f"dkc-linux-image-{flavor}-amd64"
    headers_meta = f"dkc-linux-headers-{flavor}-amd64"
    expected_graph.update({
        base: {},
        binary: {base: "${binary:Version}"},
        modules: {base: "${binary:Version}"},
        image: {
            base: "${binary:Version}",
            binary: "${binary:Version}",
            modules: "${binary:Version}",
        },
        versioned_headers: {
            base: "${binary:Version}",
            common: "${source:Version}",
            kbuild: None,
        },
        base_meta: {base: "${binary:Version}"},
        image_meta: {base_meta: "${binary:Version}", image: "${binary:Version}"},
        headers_meta: {
            base_meta: "${binary:Version}",
            versioned_headers: "${binary:Version}",
        },
    })
    expected_provides[image_meta] = {f"dkc-linux-latest-modules-{release}"}
if set(packages) != set(expected_graph):
    raise SystemExit(
        "generated package names differ from the exact 26-package graph: "
        f"missing={sorted(set(expected_graph) - set(packages))}, "
        f"unexpected={sorted(set(packages) - set(expected_graph))}"
    )

relation_pattern = re.compile(
    r"(?<![A-Za-z0-9+.-])(dkc-linux-[a-z0-9+.-]+)"
    r"(?:\s*\(([<>=]+)\s*([^)\s]+)\))?"
)
for package, fields in packages.items():
    actual = {}
    actual_provides = set()
    for field in (
        "Depends", "Pre-Depends", "Recommends", "Suggests", "Enhances",
        "Breaks", "Provides", "Conflicts", "Replaces",
    ):
        relation_groups = fields.get(field, "").split(",")
        if any("dkc-linux-" in group and "|" in group for group in relation_groups):
            raise SystemExit(f"{package} has an alternative internal relation in {field}")
        for match in relation_pattern.finditer(fields.get(field, "")):
            related, operator, version = match.groups()
            if field == "Provides":
                if operator is not None or version is not None:
                    raise SystemExit(f"{package} has versioned internal virtual Provides {related}")
                if related in actual_provides:
                    raise SystemExit(f"{package} duplicates internal Provides {related}")
                actual_provides.add(related)
                continue
            if related not in packages:
                raise SystemExit(f"{package} {field} names unknown DKC product {related}")
            if field != "Depends":
                raise SystemExit(f"{package} relates to {related} through {field}")
            if related in actual:
                raise SystemExit(f"{package} duplicates internal dependency {related}")
            actual[related] = version if operator == "=" else None
    if actual != expected_graph[package]:
        raise SystemExit(
            f"{package} internal graph differs: expected={expected_graph[package]}, actual={actual}"
        )
    if actual_provides != expected_provides.get(package, set()):
        raise SystemExit(
            f"{package} internal Provides differs: "
            f"expected={expected_provides.get(package, set())}, actual={actual_provides}"
        )

print(f"  every versioned amd64 headers package depends on clang/lld/llvm-{major}: ok")
print("  every amd64 headers meta-package requires its versioned package: ok")
print("  image/module/binary packages carry no compiler dependency: ok")
print("  all 26 generated packages have the exact reviewed internal dependency graph: ok")
PY

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
if grep -R -q 'meta-package' \
	debian/templates/image.meta.control.in \
	debian/templates/headers.meta.control.in \
	debian/templates/image-dbg.meta.control.in; then
	note "package synopses use current metapackage spelling" "FAIL"
	fail=$((fail + 1))
else
	note "package synopses use current metapackage spelling" "ok"
fi

# Renaming a binary package must not accidentally rename the Linux headers
# filesystem ABI.  Debian derives BASE_DIR and several link targets from the
# package name; without these explicit overrides the common `source` symlink is
# dangling and conventional /usr/src/linux-headers-<KREL> discovery breaks.
python3 - <<'PY'
import pathlib
import re

rules = pathlib.Path("debian/rules.real").read_text(encoding="utf-8")
required_rules = (
    "binary_kbuild: PREFIX_DIR = /usr/lib/linux-kbuild-$(ABINAME)",
    "\tdh_link $(PREFIX_DIR) /usr/src/linux-kbuild-$(ABINAME)",
    "binary_headers-common: BASE_DIR = /usr/src/linux-headers-$(ABINAME)-common$(LOCALVERSION)",
    "binary_headers: BASE_DIR = /usr/src/linux-headers-$(ABINAME)$(LOCALVERSION)",
)
for line in required_rules:
    if rules.count(line) != 1:
        raise SystemExit(f"missing exact conventional header rule: {line}")

templates = {
    "headers.install.j2": "usr/src/linux-headers-{{abiname}}{{localversion}}",
    "headers.links.j2": "usr/src/linux-headers-{{abiname}}{{localversion}}",
    "headers.featureset.links.j2": (
        "usr/src/linux-headers-{{abiname}}-common{{localversion}}"
    ),
}
for name, expected in templates.items():
    text = pathlib.Path("debian/templates", name).read_text(encoding="utf-8")
    if "usr/src/{{package}}" in text or expected not in text:
        raise SystemExit(f"{name} still derives its payload path from the renamed package")

common_links = sorted(pathlib.Path("debian").glob("dkc-linux-headers-*-common.links"))
if len(common_links) != 1:
    raise SystemExit("generated DKC common header link inventory is not unique")
match = re.fullmatch(r"dkc-linux-headers-(.+)-common[.]links", common_links[0].name)
if not match:
    raise SystemExit("cannot derive ABI from generated common header links")
abi = match.group(1)
common_text = common_links[0].read_text(encoding="utf-8")
common_pairs = {tuple(line.split()) for line in common_text.splitlines()}
for leaf in ("scripts", "tools"):
    expected = (
        f"usr/lib/linux-kbuild-{abi}/{leaf}",
        f"usr/src/linux-headers-{abi}-common/{leaf}",
    )
    if expected not in common_pairs:
        raise SystemExit(f"generated common header link is wrong: {expected}")
print("  package names are DKC while header payload/link paths remain conventional: ok")
PY

# Lifecycle scripts are part of the package contract, not incidental debhelper
# output.  Assert the pinned Debian source still generates the hooks that the
# clean package clients rely on; the matrix later rechecks the rendered .deb
# control archives with their exact KRELs.
python3 - <<'PY'
import pathlib

contracts = {
    "binary.postrm.in": ('if [ "$1" = remove ]; then', "linux-run-hooks image postrm"),
    "image.preinst.in": ("linux-run-hooks image preinst",),
    "image.postinst.in": ("linux-update-symlinks", "linux-run-hooks image postinst"),
    "image.prerm.in": ("linux-check-removal", "linux-run-hooks image prerm"),
    "image.postrm.in": (
        "linux-update-symlinks remove",
        'case "$1" in',
        "remove|purge)",
        "linux-run-hooks image postrm",
    ),
    "modules.postinst.j2": ("depmod",),
    "modules.prerm.j2": ("modules.builtin",),
    "headers.postinst.in": ("linux-run-hooks headers postinst",),
}
for name, needles in contracts.items():
    text = pathlib.Path("debian/templates", name).read_text(encoding="utf-8")
    if "version=" not in text:
        raise SystemExit(f"{name} no longer binds lifecycle actions to a KREL")
    for needle in needles:
        if needle not in text:
            raise SystemExit(f"{name} lacks lifecycle command {needle!r}")

image_postrm = pathlib.Path("debian/templates/image.postrm.in").read_text(encoding="utf-8")
remove_branch, other_actions = image_postrm.split("remove|purge)", 1)[1].split("*)", 1)
if "linux-run-hooks image postrm" in remove_branch:
    raise SystemExit("image.postrm.in runs bootloader hooks before vmlinuz removal")
if other_actions.count("linux-run-hooks image postrm") != 1:
    raise SystemExit("image.postrm.in non-removal hook contract differs")

binary_scripts = sorted(pathlib.Path("debian").glob("dkc-linux-binary-*.postrm.amd64"))
image_scripts = [
    path
    for path in sorted(pathlib.Path("debian").glob("dkc-linux-image-*.postrm.amd64"))
    if "version=" in path.read_text(encoding="utf-8")
]
if len(binary_scripts) != 3 or len(image_scripts) != 3:
    raise SystemExit(
        "generated flavor lifecycle scripts differ: "
        f"binary={len(binary_scripts)} image={len(image_scripts)}"
    )
for path in binary_scripts:
    text = path.read_text(encoding="utf-8")
    if text.count("linux-run-hooks image postrm") != 1 or 'if [ "$1" = remove ]; then' not in text:
        raise SystemExit(f"{path.name} does not own the final removal hook")
for path in image_scripts:
    text = path.read_text(encoding="utf-8")
    remove_branch, other_actions = text.split("remove|purge)", 1)[1].split("*)", 1)
    if "linux-run-hooks image postrm" in remove_branch:
        raise SystemExit(f"{path.name} runs hooks before its binary dependency is removed")
    if other_actions.count("linux-run-hooks image postrm") != 1:
        raise SystemExit(f"{path.name} non-removal hook contract differs")
print("  generated image and binary lifecycle scripts hand off removal hooks exactly once: ok")
PY

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
python3 - <<'PY'
import pathlib
import tomllib

root = pathlib.Path("debian/config/amd64")
defines = tomllib.loads((root / "defines.toml").read_text(encoding="utf-8"))
actual = [item["name"] for item in defines["flavour"]]
expected = ["v2-amd64", "v3-amd64", "v4-amd64"]
if actual != expected:
    raise SystemExit(f"unexpected Debian flavor inventory: {actual!r}")
for flavor in expected:
    path = root / f"config.{flavor}"
    if not path.is_file():
        raise SystemExit(f"missing flavor config {path}")
print("  Debian flavor inventory is exactly v2/v3/v4: ok")
packages = tomllib.loads(pathlib.Path("debian/config/defines.toml").read_text())["packages"]
expected_packages = {
    "docs": False,
    "installer": False,
    "libc_dev": False,
    "meta": True,
    "source": False,
    "tools_unversioned": False,
    "tools_versioned": True,
}
if packages != expected_packages:
    raise SystemExit(f"unexpected DKC package policy: {packages!r}")
print("  DKC package source-of-truth excludes non-products and enables metas: ok")
PY

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

PYTHONPATH=/work/src python3 - "$PWD/debian/control" "$DKC_BUILD_PROFILES" <<'PY'
import subprocess
import sys

from dkc.builddeps import all_build_depends

control = open(sys.argv[1], encoding="utf-8").read()
profiles = frozenset(sys.argv[2].split())
declared = all_build_depends(control, "amd64", profiles)
without_profiles = all_build_depends(control, "amd64", frozenset())

# Virtual packages count as installed: debhelper-compat is a Provides of
# debhelper, and reporting it missing would be a false alarm.
query = subprocess.run(
    ["dpkg-query", "-W", "-f", "${binary:Package}\t${Provides}\n"],
    capture_output=True,
    text=True,
    check=True,
).stdout
installed = set()
for line in query.splitlines():
    name, _, provides = line.partition("\t")
    installed.add(name.split(":")[0])
    for item in provides.split(","):
        item = item.strip().split()[0] if item.strip() else ""
        if item:
            installed.add(item)

missing = sorted(name for name in declared if name not in installed)
dropped = sorted(set(without_profiles) - set(declared))
print(
    f"  declared dependencies: {len(declared)} "
    f"(without profiles: {len(without_profiles)})"
)
print(f"  unresolved package names after Provides: {len(missing)}")
for name in missing:
    print(f"    {name}")
print(f"  dependencies dropped by profiles: {len(dropped)}")
for name in dropped:
    print(f"    {name}")
PY

source_version="$(dpkg-parsechangelog -SVersion)"
python3 - "$source_version" <<'PY'
import pathlib
import sys

path = pathlib.Path("debian/changelog")
entry = (
    f"dkc-linux ({sys.argv[1]}+dkc13.1) trixie; urgency=medium\n\n"
    "  * Verify the derived package release namespace.\n\n"
    " -- DKC Build Service <build@dkc.invalid>  Sat, 08 Aug 2026 00:00:00 +0000\n\n"
)
path.write_text(entry + path.read_text(encoding="utf-8"), encoding="utf-8")
PY
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
