#!/usr/bin/env bash
# Build a bounded portable kselftest tree from the exact kernel source.

set -Eeuo pipefail

[ "$#" -eq 7 ] || {
	printf 'usage: build-kselftest.sh <source> <kernel-config-dir> <evidence> <llvm-major> <identity> <flavor> <profile>\n' >&2
	exit 2
}
source_root="$1"
kernel_config_root="$2"
evidence="$3"
llvm_major="$4"
identity="$5"
flavor="$6"
profile="$7"

[[ "$llvm_major" =~ ^[0-9]+$ ]] || {
	printf 'invalid LLVM major\n' >&2
	exit 2
}
for path in "$source_root" "$kernel_config_root" "$evidence"; do
	[ -d "$path" ] || {
		printf 'required kselftest path is not a directory: %s\n' "$path" >&2
		exit 1
	}
done
case "$flavor" in
v2 | v3 | v4) ;;
*)
	printf 'invalid kselftest flavor\n' >&2
	exit 2
	;;
esac
if [ ! -f "$identity" ] || [ -L "$identity" ]; then
	printf 'publication identity is not a plain file\n' >&2
	exit 1
fi
if [ ! -f "$profile" ] || [ -L "$profile" ]; then
	printf 'kselftest profile is not a plain file\n' >&2
	exit 1
fi
# shellcheck disable=SC1090
source "$profile"
: "${DKC_KSELFTEST_PROFILE_KIND:?missing profile kind}"
: "${DKC_KSELFTEST_TARGETS:?missing target list}"
: "${DKC_KSELFTEST_TESTS:?missing test list}"
: "${DKC_KSELFTEST_V3_TESTS+x}"
: "${DKC_KSELFTEST_PER_TEST_TIMEOUT:?missing per-test timeout}"
: "${DKC_KSELFTEST_AGGREGATE_TIMEOUT:?missing aggregate timeout}"
: "${DKC_KSELFTEST_REQUIRED_BUILTIN:?missing builtin requirements}"
: "${DKC_KSELFTEST_REQUIRED_ENABLED+x}"
[[ "$DKC_KSELFTEST_PROFILE_KIND" =~ ^[a-z][a-z0-9-]*$ ]] || {
	printf 'invalid kselftest profile kind\n' >&2
	exit 2
}

if ! [[ "$DKC_KSELFTEST_PER_TEST_TIMEOUT" =~ ^[1-9][0-9]*$ ]] ||
	[ "$DKC_KSELFTEST_PER_TEST_TIMEOUT" -gt 600 ]; then
	printf 'invalid kselftest per-test timeout\n' >&2
	exit 2
fi
if ! [[ "$DKC_KSELFTEST_AGGREGATE_TIMEOUT" =~ ^[1-9][0-9]*$ ]] ||
	[ "$DKC_KSELFTEST_AGGREGATE_TIMEOUT" -gt 3600 ]; then
	printf 'invalid kselftest aggregate timeout\n' >&2
	exit 2
fi

targets_file="$evidence/kselftest-targets.txt"
tests_file="$evidence/kselftest-tests.txt"
v3_tests_file="$evidence/kselftest-v3-tests.txt"
read -r -a target_values <<<"$DKC_KSELFTEST_TARGETS"
read -r -a test_values <<<"$DKC_KSELFTEST_TESTS"
v3_test_values=()
if [ -n "$DKC_KSELFTEST_V3_TESTS" ]; then
	read -r -a v3_test_values <<<"$DKC_KSELFTEST_V3_TESTS"
fi
printf '%s\n' "${target_values[@]}" >"$targets_file"
printf '%s\n' "${test_values[@]}" >"$tests_file"
: >"$v3_tests_file"
if [ "${#v3_test_values[@]}" -gt 0 ]; then
	printf '%s\n' "${v3_test_values[@]}" >"$v3_tests_file"
fi
for value in "${target_values[@]}"; do
	[[ "$value" =~ ^[A-Za-z0-9_.+-]+(/[A-Za-z0-9_.+-]+)*$ ]] || {
		printf 'unsafe kselftest target: %s\n' "$value" >&2
		exit 1
	}
done
for value in "${v3_test_values[@]}"; do
	grep -Fxq "$value" "$tests_file" || {
		printf 'v3-only kselftest is absent from the complete profile: %s\n' "$value" >&2
		exit 1
	}
done
for value in "${test_values[@]}"; do
	[[ "$value" =~ ^[A-Za-z0-9_.+-]+(/[A-Za-z0-9_.+-]+)*:[A-Za-z0-9_.+/-]+$ ]] || {
		printf 'unsafe kselftest selector: %s\n' "$value" >&2
		exit 1
	}
done
[ "$(sort -u "$targets_file" | wc -l)" -eq "$(wc -l <"$targets_file")" ] || {
	printf 'duplicate kselftest target\n' >&2
	exit 1
}
[ "$(sort -u "$tests_file" | wc -l)" -eq "$(wc -l <"$tests_file")" ] || {
	printf 'duplicate kselftest selector\n' >&2
	exit 1
}
[ "$(sort -u "$v3_tests_file" | wc -l)" -eq "$(wc -l <"$v3_tests_file")" ] || {
	printf 'duplicate v3-only kselftest selector\n' >&2
	exit 1
}

config="$kernel_config_root/.config"
[ -f "$config" ] || {
	printf 'final kernel configuration is missing\n' >&2
	exit 1
}
for symbol in $DKC_KSELFTEST_REQUIRED_BUILTIN; do
	[[ "$symbol" =~ ^[A-Z0-9_]+$ ]]
	grep -qx "CONFIG_${symbol}=y" "$config" || {
		printf 'kselftest requires builtin CONFIG_%s\n' "$symbol" >&2
		exit 1
	}
done
for symbol in $DKC_KSELFTEST_REQUIRED_ENABLED; do
	[[ "$symbol" =~ ^[A-Z0-9_]+$ ]]
	grep -Eq "^CONFIG_${symbol}=(y|m)$" "$config" || {
		printf 'kselftest requires enabled CONFIG_%s\n' "$symbol" >&2
		exit 1
	}
done

install_root=/work/kselftest-install
selftest_object=/work/kselftest-object
test ! -e "$install_root" || {
	printf 'stale kselftest install root exists\n' >&2
	exit 1
}
test ! -e "$selftest_object" || {
	printf 'stale kselftest object root exists\n' >&2
	exit 1
}
mkdir "$install_root" "$selftest_object"
build_log="$evidence/kselftest-build.log"

if ! timeout --signal=TERM --kill-after=30s 15m \
	make -C "$source_root" O="$selftest_object" ARCH=x86 \
	LLVM="-${llvm_major}" headers >"$build_log" 2>&1; then
	tail -n 100 "$build_log" >&2
	printf 'kernel UAPI header preparation for kselftest failed\n' >&2
	exit 1
fi
if ! timeout --signal=TERM --kill-after=30s 15m \
	make -C "$source_root/tools/testing/selftests" \
	O="$selftest_object" ARCH=x86 LLVM="-${llvm_major}" \
	CC="clang-${llvm_major}" HOSTCC="clang-${llvm_major}" \
	TARGETS="$DKC_KSELFTEST_TARGETS" SKIP_TARGETS= FORCE_TARGETS=1 \
	install INSTALL_PATH="$install_root" >>"$build_log" 2>&1; then
	tail -n 140 "$build_log" >&2
	printf 'selected kselftest collections did not all build\n' >&2
	exit 1
fi

if grep -Fxq 'ptrace:vmaccess-only' "$tests_file"; then
	[ -x "$install_root/ptrace/vmaccess" ] || {
		printf 'vmaccess executable required by its selected wrapper is missing\n' >&2
		exit 1
	}
	install -m 0755 /work/repo/tests/integration/kselftest-wrappers/ptrace-vmaccess-only \
		"$install_root/ptrace/vmaccess-only"
	grep -Fxq 'ptrace:vmaccess-only' "$install_root/kselftest-list.txt" ||
		printf '%s\n' 'ptrace:vmaccess-only' >>"$install_root/kselftest-list.txt"
fi

if [ ! -x "$install_root/run_kselftest.sh" ] ||
	[ ! -s "$install_root/kselftest-list.txt" ]; then
	printf 'portable kselftest runner or inventory is missing\n' >&2
	exit 1
fi
while IFS= read -r test_name; do
	grep -Fxq "$test_name" "$install_root/kselftest-list.txt" || {
		printf 'selected kselftest was not installed: %s\n' "$test_name" >&2
		exit 1
	}
done <"$tests_file"

if find "$install_root" \( -type l -o ! -type d ! -type f \) -print -quit | grep -q .; then
	printf 'portable kselftest tree contains a link or special file\n' >&2
	exit 1
fi
mkdir "$install_root/dkc-profile"
cp "$targets_file" "$install_root/dkc-profile/targets.txt"
cp "$tests_file" "$install_root/dkc-profile/tests.txt"
cp "$v3_tests_file" "$install_root/dkc-profile/v3-tests.txt"
cat >"$install_root/dkc-profile/runtime.env" <<EOF
per_test_timeout=${DKC_KSELFTEST_PER_TEST_TIMEOUT}
aggregate_timeout=${DKC_KSELFTEST_AGGREGATE_TIMEOUT}
EOF

# Some upstream collection install rules preserve a root-only umask. The tree
# is portable only if tests that deliberately switch to an unprivileged uid can
# traverse it and re-execute their installed binaries.
chmod -R a+rX "$install_root"
if find "$install_root" -type d ! -perm -0005 -print -quit | grep -q . ||
	find "$install_root" -type f ! -perm -0004 -print -quit | grep -q . ||
	find "$install_root" -type f -perm -0100 ! -perm -0001 -print -quit | grep -q .; then
	printf 'portable kselftest tree has inaccessible modes\n' >&2
	exit 1
fi

(
	cd "$install_root"
	find . -type f -print0 | sort -z | xargs -0 sha256sum
) >"$evidence/kselftest-files.sha256"
bundle="$evidence/kselftest.tar.xz"
tar --sort=name --mtime="@${SOURCE_DATE_EPOCH}" --clamp-mtime \
	--owner=0 --group=0 --numeric-owner \
	--create --file=- --directory="$install_root" . |
	xz --threads=1 --check=sha256 -1 >"$bundle"
(
	cd "$evidence"
	sha256sum kselftest.tar.xz >kselftest.tar.xz.sha256
	sha256sum kselftest-build.log >kselftest-build.log.sha256
)
xz --threads=1 --check=sha256 -1 "$build_log"

python3 - "$profile" "$config" "$evidence" "$llvm_major" "$install_root" \
	"$identity" "$flavor" "$DKC_KSELFTEST_PROFILE_KIND" <<'PY'
import hashlib
import json
import pathlib
import sys

profile = pathlib.Path(sys.argv[1])
config = pathlib.Path(sys.argv[2])
evidence = pathlib.Path(sys.argv[3])
llvm_major = int(sys.argv[4])
install_root = pathlib.Path(sys.argv[5])
identity_path = pathlib.Path(sys.argv[6])
flavor = sys.argv[7]
profile_kind = sys.argv[8]


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


targets = (evidence / "kselftest-targets.txt").read_text(encoding="utf-8").splitlines()
tests = (evidence / "kselftest-tests.txt").read_text(encoding="utf-8").splitlines()
v3_tests = (evidence / "kselftest-v3-tests.txt").read_text(encoding="utf-8").splitlines()
runtime = {}
for line in (install_root / "dkc-profile/runtime.env").read_text(
    encoding="utf-8"
).splitlines():
    key, separator, value = line.partition("=")
    if not separator or key in runtime or not value.isdigit():
        raise SystemExit("malformed kselftest runtime profile")
    runtime[key] = int(value)
if set(runtime) != {"per_test_timeout", "aggregate_timeout"}:
    raise SystemExit("incomplete kselftest runtime profile")

bundle = evidence / "kselftest.tar.xz"
file_manifest = evidence / "kselftest-files.sha256"
identity = json.loads(identity_path.read_text(encoding="utf-8"))
kernel_release = identity.get("kernel_releases", {}).get(flavor)
build_input_digest = identity.get("build_input_digest")
lto_mode = identity.get("lto_mode")
if (
    not isinstance(kernel_release, str)
    or not isinstance(build_input_digest, str)
    or len(build_input_digest) != 64
    or lto_mode not in ("none", "thin", "full")
):
    raise SystemExit("publication identity lacks the selected kselftest build identity")
report = {
    "schema_version": 2,
    "status": "PASS",
    "framework": "Linux kselftest",
    "profile_kind": profile_kind,
    "flavor": flavor,
    "kernel_release": kernel_release,
    "build_input_digest": build_input_digest,
    "lto_mode": lto_mode,
    "llvm_major": llvm_major,
    "target_count": len(targets),
    "test_count": len(tests),
    "targets": targets,
    "tests": tests,
    "v3_test_count": len(v3_tests),
    "v3_tests": v3_tests,
    "per_test_timeout_seconds": runtime["per_test_timeout"],
    "aggregate_timeout_seconds": runtime["aggregate_timeout"],
    "profile_sha256": sha256(profile),
    "kernel_config_sha256": sha256(config),
    "file_manifest_sha256": sha256(file_manifest),
    "bundle_sha256": sha256(bundle),
    "bundle_size": bundle.stat().st_size,
}
(evidence / "kselftest-build.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

printf 'kselftest build PASS: %s targets, %s selected tests, %s-byte bundle\n' \
	"$(wc -l <"$targets_file")" "$(wc -l <"$tests_file")" "$(stat -c %s "$bundle")" >&2
