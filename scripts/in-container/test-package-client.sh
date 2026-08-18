#!/usr/bin/env bash
# Exercise real APT, kernel hooks, headers, and DKMS in a clean Debian client.

set -Eeuo pipefail

if [ "$#" -ne 2 ] || [[ ! "$1" =~ ^(image|headers)$ ]] || [[ ! "$2" =~ ^[0-9]+$ ]]; then
	echo "usage: test-package-client.sh <image|headers> <llvm-major>" >&2
	exit 2
fi
mode="$1"
llvm_major="$2"
log="/evidence/apt-${mode}.log"

[ "$(id -u)" -eq 0 ] || {
	echo "package client must run as container root" >&2
	exit 1
}
test -f /matrix/repository/Packages
test -f /matrix/repository/Sources
test -d /repo/tests/integration/dkms-fixture/package
mkdir -p /evidence
: >"$log"

run_logged() {
	local label="$1"
	shift
	printf '\n=== %s ===\n' "$label" >>"$log"
	if "$@" >>"$log" 2>&1; then
		return 0
	fi
	echo "package client failed during: $label" >&2
	tail -n 120 "$log" >&2
	return 1
}

export DEBIAN_FRONTEND=noninteractive
export SYSTEMD_OFFLINE=1
printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
chmod 755 /usr/sbin/policy-rc.d
rm -f /etc/apt/apt.conf.d/docker-clean

# The base must be a plain Trixie client.  Backports is added only in the
# headers mode, and the DKC repository is the local matrix under test.
if grep -RhiE '(^|[[:space:]])(sid|unstable)([[:space:]]|$)|apt\.llvm\.org' \
	/etc/apt/sources.list /etc/apt/sources.list.d 2>/dev/null; then
	echo "base client contains a forbidden repository" >&2
	exit 1
fi
printf '%s\n' \
	'Types: deb deb-src' \
	'URIs: file:/matrix/repository' \
	'Suites: ./' \
	'Trusted: yes' \
	>/etc/apt/sources.list.d/dkc-local.sources
if [ "$mode" = headers ]; then
	printf '%s\n' \
		'Types: deb' \
		'URIs: http://deb.debian.org/debian' \
		'Suites: trixie-backports' \
		'Components: main' \
		'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \
		>/etc/apt/sources.list.d/backports.sources
fi

run_logged "apt update" apt-get -o Acquire::Languages=none update
# Make every inherited base package resolve to the authenticated indices used
# by this client, rather than leaving an old status-only version unattributed.
run_logged "base upgrade" apt-get -y --no-install-recommends upgrade
run_logged "stock Debian kernel" apt-get install -y --no-install-recommends \
	linux-image-amd64 initramfs-tools

if [ "$mode" = headers ]; then
	run_logged "source-package client" apt-get install -y --no-install-recommends dpkg-dev
	mkdir /tmp/source-client
	(
		cd /tmp/source-client
		run_logged "download and extract dkc-linux source" apt-get source dkc-linux
	)
	mapfile -t extracted_source_roots < <(
		find /tmp/source-client -mindepth 1 -maxdepth 1 -type d \
			-name 'dkc-linux-*' -print
	)
	if [ "${#extracted_source_roots[@]}" -ne 1 ] ||
		! test -f "${extracted_source_roots[0]}/debian/dkc/publication-identity.json"; then
		echo "apt-get source did not reconstruct the final downstream source" >&2
		exit 1
	fi
	run_logged "DKMS framework" apt-get install -y --no-install-recommends dkms
	cp -a /repo/tests/integration/dkms-fixture/package /tmp/dkc-dkms-fixture
	run_logged "build controlled DKMS package" dpkg-deb --build \
		/tmp/dkc-dkms-fixture /tmp/dkc-dkms-fixture_1.0_all.deb
	sha256sum /tmp/dkc-dkms-fixture_1.0_all.deb >/evidence/dkms-fixture.sha256
	run_logged "install controlled DKMS package before DKC" apt-get install -y \
		/tmp/dkc-dkms-fixture_1.0_all.deb
fi

image_metas=(
	dkc-linux-image-v2-amd64
	dkc-linux-image-v3-amd64
)
all_metas=(
	dkc-linux-base-v2-amd64 dkc-linux-image-v2-amd64 dkc-linux-headers-v2-amd64
	dkc-linux-base-v3-amd64 dkc-linux-image-v3-amd64 dkc-linux-headers-v3-amd64
)
mapfile -t repository_packages < <(
	sed -n 's/^Package: //p' /matrix/repository/Packages | sort -u
)
[ "${#repository_packages[@]}" -eq 18 ] || {
	echo "local DKC repository does not contain exactly 18 release packages" >&2
	exit 1
}
if [ "$mode" = headers ]; then
	# This is the complete product-union client: install every generated .deb in
	# one dpkg database, not only a dependency-selected subset.  The image-only
	# client separately proves the normal meta-package dependency path.
	run_logged "install the complete 18-package DKC union" \
		apt-get install -y --no-install-recommends "${repository_packages[@]}"
else
	run_logged "install all DKC image flavors" apt-get install -y --no-install-recommends \
		"${image_metas[@]}"
fi

# Normalize the complete-union client's manual/automatic state to what the
# stable meta-package interface promises.  The explicit 18-package install
# above proves coexistence; this transition separately proves APT retention and
# removal through only the six operator-facing meta-packages.
if [ "$mode" = headers ]; then
	run_logged "mark versioned package closure automatic" apt-mark auto "${repository_packages[@]}"
	run_logged "retain only stable DKC meta-packages manually" apt-mark manual "${all_metas[@]}"
fi

mapfile -t dkc_krels < <(
	for flavor in v2 v3; do
		sed -n "s/^Package: dkc-linux-image-\\(.*-${flavor}-amd64\\)$/\\1/p" \
			/matrix/repository/Packages
	done
)
[ "${#dkc_krels[@]}" -eq 2 ] || {
	printf 'expected two DKC module trees, found: %s\n' "${dkc_krels[*]-}" >&2
	exit 1
}
for flavor in v2 v3; do
	count="$(printf '%s\n' "${dkc_krels[@]}" | grep -c -- "-${flavor}-amd64$")"
	[ "$count" -eq 1 ] || {
		echo "DKC module trees do not contain exactly one ${flavor}" >&2
		exit 1
	}
done
mapfile -t stock_krels < <(
	while IFS= read -r candidate; do
		is_dkc=false
		for dkc_krel in "${dkc_krels[@]}"; do
			if [ "$candidate" = "$dkc_krel" ]; then
				is_dkc=true
				break
			fi
		done
		[ "$is_dkc" = true ] || printf '%s\n' "$candidate"
	done < <(find /lib/modules -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort)
)
[ "${#stock_krels[@]}" -ge 1 ] || {
	echo "stock Debian kernel did not coexist with DKC" >&2
	exit 1
}

for krel in "${dkc_krels[@]}"; do
	test -s "/boot/vmlinuz-${krel}"
	test -s "/boot/config-${krel}"
	test -s "/boot/System.map-${krel}"
	test -s "/boot/initrd.img-${krel}"
	test -d "/lib/modules/${krel}/kernel"
done
if [ "$mode" = headers ]; then
	installed_manifest=/evidence/dkc-installed-files.txt
	: >"$installed_manifest"
	for package in "${repository_packages[@]}"; do
		[ "$(dpkg-query -W -f='${db:Status-Status}' "$package")" = installed ] || {
			echo "complete matrix package is not installed: ${package}" >&2
			exit 1
		}
		printf '### %s\n' "$package" >>"$installed_manifest"
		dpkg-query -L "$package" >>"$installed_manifest"
	done
fi
dpkg-query -W -f='${binary:Package}\t${db:Status-Status}\n' |
	awk -F '\t' '$2 == "installed" { print $1 }' | sort >"/evidence/installed-${mode}.txt"
apt-mark showmanual | sort >"/evidence/manual-${mode}.txt"
apt-get -s autoremove >"/evidence/autoremove-before-${mode}.txt"
mapfile -t initial_removals < <(
	sed -n 's/^Remv \([^ ]*\).*/\1/p' "/evidence/autoremove-before-${mode}.txt" | sort -u
)
[ "${#initial_removals[@]}" -eq 0 ] || {
	echo "clean client has autoremove candidates before flavor removal: ${initial_removals[*]}" >&2
	exit 1
}
for package in "${image_metas[@]}"; do
	grep -qx "$package" "/evidence/manual-${mode}.txt" || {
		echo "requested meta-package is not manual: $package" >&2
		exit 1
	}
done

if [ "$mode" = image ]; then
	if grep -Eq '^(clang|lld|llvm)(-[0-9]+)?$' /evidence/installed-image.txt; then
		echo "image-only closure installed an LLVM compiler/tool package" >&2
		exit 1
	fi
else
	for tool in clang lld llvm; do
		grep -qx "${tool}-${llvm_major}" /evidence/installed-headers.txt || {
			echo "headers did not install ${tool}-${llvm_major}" >&2
			exit 1
		}
	done

	tool_report=/evidence/header-tools.txt
	: >"$tool_report"
	for tool in clang clang++ ld.lld llvm-ar llvm-nm llvm-objcopy llvm-objdump llvm-readelf llvm-strip llvm-link; do
		resolved="$(command -v "${tool}-${llvm_major}")"
		real="$(readlink -f "$resolved")"
		case "$real" in
		/usr/bin/* | /usr/lib/llvm-${llvm_major}/bin/*) ;;
		*)
			echo "unexpected ${tool} resolution: ${real}" >&2
			exit 1
			;;
		esac
		printf '%s\t%s\n' "${tool}-${llvm_major}" "$real" >>"$tool_report"
	done

	for krel in "${dkc_krels[@]}"; do
		build="/lib/modules/${krel}/build"
		source_link="/lib/modules/${krel}/source"
		abi="${krel%-v[234]-amd64}"
		expected_build="/usr/src/linux-headers-${krel}"
		expected_source="/usr/src/linux-headers-${abi}-common"
		test -L "$build"
		test -L "$source_link"
		[ "$(readlink -f "$build")" = "$expected_build" ] || {
			echo "${krel} build link does not resolve to ${expected_build}" >&2
			exit 1
		}
		[ "$(readlink -f "$source_link")" = "$expected_source" ] || {
			echo "${krel} source link does not resolve to ${expected_source}" >&2
			exit 1
		}
		test -d "$expected_build"
		test -d "$expected_source"
		[ "$(dpkg-query -S "/usr/lib/modules/${krel}/build" | cut -d: -f1)" = \
			"dkc-linux-headers-${krel}" ] || {
			echo "${krel} build link has the wrong package owner" >&2
			exit 1
		}
		[ "$(dpkg-query -S "$expected_source/Makefile" | cut -d: -f1)" = \
			"dkc-linux-headers-${abi}-common" ] || {
			echo "${krel} common Makefile has the wrong package owner" >&2
			exit 1
		}
		test -e "$build/Module.symvers"
		test -e "$build/include/generated/autoconf.h"
		test -e "$source_link/Makefile"
		variables="$build/.kernelvariables"
		for assignment in \
			"LLVM = -${llvm_major}" \
			"LLVM_PREFIX = " \
			"LLVM_SUFFIX = -${llvm_major}" \
			"CC = .*clang-${llvm_major}" \
			"HOSTCC = clang-${llvm_major}" \
			"HOSTCXX = clang[+][+]-${llvm_major}" \
			"LD = ld[.]lld-${llvm_major}" \
			"AR = llvm-ar-${llvm_major}" \
			"NM = llvm-nm-${llvm_major}" \
			"OBJCOPY = llvm-objcopy-${llvm_major}" \
			"OBJDUMP = llvm-objdump-${llvm_major}" \
			"READELF = llvm-readelf-${llvm_major}" \
			"STRIP = llvm-strip-${llvm_major}" \
			"LLVM_LINK = llvm-link-${llvm_major}"; do
			grep -Eq "^${assignment}$" "$variables" || {
				echo "${krel} lacks header tool assignment: ${assignment}" >&2
				exit 1
			}
		done
		plain="/tmp/plain-${krel}"
		mkdir "$plain"
		cp /usr/src/dkc-fixture-1.0/Makefile /usr/src/dkc-fixture-1.0/dkc_fixture.c "$plain/"
		module_log="/evidence/plain-${krel}.log"
		if ! make -C "$build" M="$plain" V=1 modules >"$module_log" 2>&1; then
			tail -n 100 "$module_log" >&2
			exit 1
		fi
		grep -Eq "(^|[ /])clang-${llvm_major}([[:space:]]|$)" "$module_log"
		grep -Eq "(^|[ /])ld[.]lld-${llvm_major}([[:space:]]|$)" "$module_log"
		plain_vermagic="$(modinfo -F vermagic "$plain/dkc_fixture.ko")"
		[[ "$plain_vermagic" == "$krel "* ]] || {
			echo "plain module vermagic mismatch for ${krel}: ${plain_vermagic}" >&2
			exit 1
		}

		dkms status -m dkc-fixture -v 1.0 -k "$krel" |
			grep -q "^dkc-fixture/1.0, ${krel}.*: installed$" || {
			echo "DKMS did not install the fixture for ${krel}" >&2
			exit 1
		}
		dkms_module="$(find "/lib/modules/${krel}/updates/dkms" -maxdepth 1 \
			-type f -name 'dkc_fixture.ko*' -print -quit)"
		test -n "$dkms_module"
		dkms_vermagic="$(modinfo -F vermagic "$dkms_module")"
		[[ "$dkms_vermagic" == "$krel "* ]] || {
			echo "DKMS module vermagic mismatch for ${krel}: ${dkms_vermagic}" >&2
			exit 1
		}
		dkms_log="$(find "/var/lib/dkms/dkc-fixture/1.0/${krel}" -type f \
			-path '*/log/make.log' -print -quit)"
		test -n "$dkms_log"
		grep -Eq "(^|[ /])clang-${llvm_major}([[:space:]]|$)" "$dkms_log" || {
			echo "DKMS log for ${krel} does not prove clang-${llvm_major}" >&2
			exit 1
		}
		grep -Eq "(^|[ /])ld[.]lld-${llvm_major}([[:space:]]|$)" "$dkms_log" || {
			echo "DKMS log for ${krel} does not prove ld.lld-${llvm_major}" >&2
			exit 1
		}
		cp "$dkms_log" "/evidence/dkms-${krel}.log"
	done
	dkms status >/evidence/dkms-status-before-removal.txt

	# GCC is an explicitly documented compatibility attempt, not the supported
	# default.  Record either outcome without disguising it as the LLVM proof.
	gcc_krel="${dkc_krels[0]}"
	gcc_dir="/tmp/gcc-override"
	mkdir "$gcc_dir"
	cp /usr/src/dkc-fixture-1.0/Makefile /usr/src/dkc-fixture-1.0/dkc_fixture.c "$gcc_dir/"
	if make -C "/lib/modules/${gcc_krel}/build" M="$gcc_dir" V=1 \
		CC=gcc LD=ld.bfd modules >/evidence/gcc-override.log 2>&1; then
		gcc_override=PASS
		gcc_vermagic="$(modinfo -F vermagic "$gcc_dir/dkc_fixture.ko")"
		[[ "$gcc_vermagic" == "$gcc_krel "* ]]
	else
		gcc_override=UNSUPPORTED
	fi
	printf 'gcc_override=%s\n' "$gcc_override" >/evidence/gcc-override.env
fi

# Record every installed package's APT policy, then reject any Sid or
# third-party URI.  Local DKC packages must resolve through the flat file repo.
policy_report="/evidence/apt-policy-${mode}.txt"
: >"$policy_report"
while IFS= read -r package; do
	printf '\n### %s\n' "$package" >>"$policy_report"
	apt-cache policy "$package" >>"$policy_report"
done <"/evidence/installed-${mode}.txt"
if grep -Eqi '(^|[ /])(sid|unstable)([ /]|$)|apt[.]llvm[.]org' "$policy_report"; then
	echo "Sid or a third-party origin entered the client closure" >&2
	exit 1
fi
while IFS= read -r uri; do
	case "$uri" in
	http://deb.debian.org/* | https://deb.debian.org/* | http://security.debian.org/* | https://security.debian.org/* | file:/matrix/repository | file:/matrix/repository/*) ;;
	*)
		echo "unreviewed package origin URI: ${uri}" >&2
		exit 1
		;;
	esac
done < <(grep -Eo '(https?|file):/[^ ]+' "$policy_report" | sort -u)
origin_report="/evidence/installed-origins-${mode}.tsv"
: >"$origin_report"
while IFS= read -r package; do
	version="$(dpkg-query -W -f='${Version}' "$package")"
	if [ "$package" = dkc-dkms-fixture ]; then
		if [ "$mode" != headers ] || [ "$version" != 1.0 ]; then
			echo "controlled DKMS fixture escaped its expected client/version" >&2
			exit 1
		fi
		printf '%s\t%s\t%s\n' "$package" "$version" controlled-fixture >>"$origin_report"
		continue
	fi
	mapfile -t uri_lines < <(apt-get --print-uris download "${package}=${version}" 2>>"$log" | sed -n "/^'/p")
	[ "${#uri_lines[@]}" -eq 1 ] || {
		echo "installed package has no unique downloadable origin: ${package}=${version}" >&2
		exit 1
	}
	uri="${uri_lines[0]#\'}"
	uri="${uri%%\'*}"
	case "$uri" in
	http://deb.debian.org/* | https://deb.debian.org/* | http://security.debian.org/* | https://security.debian.org/* | file:/matrix/repository | file:/matrix/repository/*) ;;
	*)
		echo "unreviewed installed package origin: ${package} ${uri}" >&2
		exit 1
		;;
	esac
	printf '%s\t%s\t%s\n' "$package" "$version" "$uri" >>"$origin_report"
done <"/evidence/installed-${mode}.txt"
for package in "${repository_packages[@]}"; do
	if ! dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
		continue
	fi
	awk -F '\t' -v package="$package" '$1 == package && $3 ~ /^file:\/matrix\/repository\// { found=1 } END { exit !found }' \
		"$origin_report" || {
		echo "${package} was not downloadable from the DKC test repository" >&2
		exit 1
	}
done
for package in "${image_metas[@]}"; do
	apt-cache policy "$package" | grep -q 'file:/matrix/repository' || {
		echo "${package} did not resolve through the DKC test repository" >&2
		exit 1
	}
done
if [ "$mode" = headers ]; then
	for tool in clang lld llvm; do
		apt-cache policy "${tool}-${llvm_major}" | grep -q 'trixie-backports' || {
			echo "${tool}-${llvm_major} did not resolve from Debian backports" >&2
			exit 1
		}
	done
fi

v3_krel="$(printf '%s\n' "${dkc_krels[@]}" | grep -- '-v3-amd64$')"
v3_metas=(dkc-linux-base-v3-amd64 dkc-linux-image-v3-amd64 dkc-linux-headers-v3-amd64)
mapfile -t v3_versioned < <(
	while IFS= read -r package; do
		if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
			printf '%s\n' "$package"
		fi
	done < <(printf '%s\n' "${repository_packages[@]}" | grep -- "-${v3_krel}$" | sort)
)
expected_v3_versioned=$([ "$mode" = headers ] && echo 5 || echo 4)
[ "${#v3_versioned[@]}" -eq "$expected_v3_versioned" ]
run_logged "remove only the v3 stable meta-packages" apt-get purge -y "${v3_metas[@]}"
apt-get -s autoremove >"/evidence/autoremove-v3-candidates-${mode}.txt"
mapfile -t proposed_removals < <(
	sed -n 's/^Remv \([^ ]*\).*/\1/p' "/evidence/autoremove-v3-candidates-${mode}.txt" |
		sort -u
)
if [ "$(printf '%s\n' "${proposed_removals[@]}" | sort)" != \
	"$(printf '%s\n' "${v3_versioned[@]}" | sort)" ]; then
	echo "APT autoremove candidates differ from the exact v3 versioned closure" >&2
	printf 'expected: %s\nactual: %s\n' "${v3_versioned[*]}" "${proposed_removals[*]}" >&2
	exit 1
fi
run_logged "autoremove after v3 removal" apt-get autoremove -y --purge
test ! -e "/lib/modules/${v3_krel}"
test ! -e "/boot/vmlinuz-${v3_krel}"
test ! -e "/boot/config-${v3_krel}"
test ! -e "/boot/System.map-${v3_krel}"
test ! -e "/boot/initrd.img-${v3_krel}"
test ! -e "/usr/src/linux-headers-${v3_krel}"
for package in "${v3_metas[@]}"; do
	if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
		echo "v3 removal left a stable meta-package installed: ${package}" >&2
		exit 1
	fi
done
for package in "${v3_versioned[@]}"; do
	if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
		echo "v3 autoremove left a versioned package installed: ${package}" >&2
		exit 1
	fi
done
v2_krel="$(printf '%s\n' "${dkc_krels[@]}" | grep -- '-v2-amd64$')"
test -s "/boot/vmlinuz-${v2_krel}"
test -s "/boot/config-${v2_krel}"
test -s "/boot/System.map-${v2_krel}"
test -s "/boot/initrd.img-${v2_krel}"
test -d "/lib/modules/${v2_krel}/kernel"
for krel in "${stock_krels[@]}"; do
	test -d "/lib/modules/${krel}"
	test -s "/boot/vmlinuz-${krel}"
	test -s "/boot/initrd.img-${krel}"
done
if [ "$mode" = headers ]; then
	if dkms status -m dkc-fixture -v 1.0 -k "$v3_krel" | grep -q ': installed$'; then
		echo "DKMS still reports the removed v3 kernel as installed" >&2
		exit 1
	fi
	dkms status -m dkc-fixture -v 1.0 -k "$v2_krel" | grep -q ': installed$'
	dkms status >/evidence/dkms-status-after-removal.txt
fi
apt-get -s autoremove >"/evidence/autoremove-after-${mode}.txt"
if grep -q '^Remv ' "/evidence/autoremove-after-${mode}.txt"; then
	echo "clean client still has autoremove candidates after v3 removal" >&2
	exit 1
fi
dpkg-query -W -f='${binary:Package}\t${db:Status-Status}\n' |
	awk -F '\t' '$2 == "installed" { print $1 }' |
	sort >"/evidence/installed-after-removal-${mode}.txt"

cat >"/evidence/result-${mode}.env" <<EOF
status=PASS
mode=${mode}
stock_kernel_coexistence=PASS
release_flavor_coexistence=PASS
v3_removal_isolation=PASS
initramfs_lifecycle=PASS
headers_plain_module=$([ "$mode" = headers ] && echo PASS || echo NOT_RUN)
dkms_lifecycle=$([ "$mode" = headers ] && echo PASS || echo NOT_RUN)
complete_18_package_union=$([ "$mode" = headers ] && echo PASS || echo NOT_RUN)
EOF
echo "${mode} package client PASS" >&2
