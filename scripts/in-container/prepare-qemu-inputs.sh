#!/usr/bin/env bash
# Prepare direct package inputs, cloud-init data, and a result disk for QEMU.

set -Eeuo pipefail

[ "$#" -eq 4 ] || {
	printf 'usage: prepare-qemu-inputs.sh <stage> <flavor> <result-root> <kselftest-root>\n' >&2
	exit 2
}
stage="$1"
flavor="$2"
root="$3"
selftest_root="$4"

case "$flavor" in
v2 | v3 | v4) ;;
*)
	printf 'invalid flavor: %s\n' "$flavor" >&2
	exit 2
	;;
esac
[ -d "$root/artifacts" ]
[ -d "$root/evidence" ]
[ -d "$selftest_root/evidence" ]

for command in python3 dpkg-deb xorriso mkfs.ext4 truncate jq sha256sum; do
	command -v "$command" >/dev/null || {
		printf 'missing command: %s\n' "$command" >&2
		exit 1
	}
done

iso_root="$stage/iso-root"
packages="$iso_root/packages"
support="$iso_root/support"
evidence="$stage/evidence"
scenario="$stage/scenarios/$flavor"
seed="$scenario/seed"
mkdir -p "$packages" "$support" "$evidence" "$seed"

python3 scripts/in-container/audit-package-matrix.py --flavor \
	"$flavor" "$root" "$packages" "$evidence/package-audit.json"
[ "$(find "$packages" -maxdepth 1 -type f -name '*.deb' | wc -l)" -eq 10 ]
[ "$(find "$packages" -mindepth 1 -maxdepth 1 -type f ! -name '*.deb' | wc -l)" -eq 0 ]

fixture="$packages/dkc-dkms-fixture_1.0_all.deb"
dpkg-deb --root-owner-group --build tests/integration/dkms-fixture/package "$fixture" >/dev/null
install -m 0755 scripts/dkc-cpu-select "$support/dkc-cpu-select"
install -m 0755 tests/integration/qemu/guest-validate.sh "$support/guest-validate.sh"

selftest_report="$selftest_root/evidence/kselftest-build.json"
selftest_bundle="$selftest_root/evidence/kselftest.tar.xz"
selftest_result="$selftest_root/evidence/result.env"
grep -qx 'status=PASS' "$selftest_result"
grep -qx "flavor=${flavor}" "$selftest_result"
grep -qx 'profile_kind=qualification' "$selftest_result"
(
	cd "$selftest_root/evidence"
	sha256sum --check evidence.sha256 >/dev/null
)
selftest_sha="$(jq -er \
	--arg flavor "$flavor" --arg kind qualification \
	'select(.schema_version == 2 and .status == "PASS" and .framework == "Linux kselftest" and .flavor == $flavor and .profile_kind == $kind) | .bundle_sha256' \
	"$selftest_report")"
[[ "$selftest_sha" =~ ^[0-9a-f]{64}$ ]]
[ "$(sha256sum "$selftest_bundle" | awk '{print $1}')" = "$selftest_sha" ] || {
	printf 'kselftest bundle differs from the separate %s evidence\n' "$flavor" >&2
	exit 1
}
kernel_release="$(jq -er --arg flavor "$flavor" \
	'select(.status == "PASS" and .flavor == $flavor) | .kernel_release' \
	"$root/evidence/attestation.json")"
jq -e --arg flavor "$flavor" --arg release "$kernel_release" \
	--slurpfile attestation "$root/evidence/attestation.json" \
	--slurpfile identity "$root/evidence/publication-identity.json" \
	'select(.flavor == $flavor and .kernel_release == $release and
		.build_input_digest == $identity[0].build_input_digest and
		.lto_mode == $identity[0].lto_mode and
		.lto_mode == $attestation[0].lto_mode and
		.kernel_config_sha256 == $attestation[0].shipped_config_sha256 and
		.llvm_major == $attestation[0].llvm_major)' "$selftest_report" >/dev/null
cp "$selftest_bundle" "$support/kselftest.tar.xz"
printf '%s  kselftest.tar.xz\n' "$selftest_sha" >"$support/kselftest.tar.xz.sha256"
(
	cd "$packages"
	sha256sum -- *.deb
) >"$support/packages.sha256"

xorriso -as mkisofs -quiet -r -J -V DKC_INPUTS -o "$stage/inputs.iso" "$iso_root"
(
	cd "$stage"
	sha256sum inputs.iso
) >"$evidence/inputs.iso.sha256"

[[ "$kernel_release" =~ ^[A-Za-z0-9.+~-]+-${flavor}-amd64$ ]] || {
	printf 'invalid attested kernel release for %s: %s\n' "$flavor" "$kernel_release" >&2
	exit 1
}

cat >"$seed/meta-data" <<EOF
instance-id: dkc-${DKC_RUN_ID}-${flavor}
local-hostname: dkc-${flavor}
EOF
cat >"$seed/user-data" <<EOF
#cloud-config
ssh_pwauth: false
disable_root: true
write_files:
  - path: /etc/dkc-vm-test.env
    owner: root:root
    permissions: '0600'
    content: |
      DKC_TEST_FLAVOR='${flavor}'
      DKC_TEST_KERNEL_RELEASE='${kernel_release}'
      DKC_TEST_PACKAGES='/mnt/dkc-inputs/packages'
      DKC_TEST_SUPPORT='/mnt/dkc-inputs/support'
      DKC_TEST_RESULTS='/var/lib/dkc-results'
      DKC_TEST_KSELFTEST_BUNDLE='/mnt/dkc-inputs/support/kselftest.tar.xz'
      DKC_TEST_KSELFTEST_SHA256='/mnt/dkc-inputs/support/kselftest.tar.xz.sha256'
  - path: /etc/systemd/system/dkc-vm-test.service
    owner: root:root
    permissions: '0644'
    content: |
      [Unit]
      Description=DKC kernel boot validation
      After=local-fs.target network-online.target
      Wants=network-online.target
      RequiresMountsFor=/mnt/dkc-inputs /var/lib/dkc-results

      [Service]
      Type=oneshot
      ExecStart=/usr/local/sbin/dkc-vm-guest-validate
      TimeoutStartSec=40min

      [Install]
      WantedBy=multi-user.target
runcmd:
  - [mkdir, -p, /mnt/dkc-inputs, /var/lib/dkc-results]
  - [mount, -L, DKC_INPUTS, /mnt/dkc-inputs]
  - [mount, -L, DKC_RESULTS, /var/lib/dkc-results]
  - [install, -m, '0755', /mnt/dkc-inputs/support/dkc-cpu-select, /usr/local/bin/dkc-cpu-select]
  - [install, -m, '0755', /mnt/dkc-inputs/support/guest-validate.sh, /usr/local/sbin/dkc-vm-guest-validate]
  - [sh, -c, "printf '%s\\n' 'LABEL=DKC_INPUTS /mnt/dkc-inputs iso9660 ro,nofail 0 0' 'LABEL=DKC_RESULTS /var/lib/dkc-results ext4 defaults,nofail 0 0' >> /etc/fstab"]
  - [systemctl, daemon-reload]
  - [systemctl, enable, dkc-vm-test.service]
  - [systemctl, start, --no-block, dkc-vm-test.service]
EOF

xorriso -as mkisofs -quiet -r -J -V cidata -o "$scenario/seed.iso" "$seed"
truncate -s 256M "$scenario/results.img"
mkfs.ext4 -q -F -L DKC_RESULTS "$scenario/results.img"
cat >"$scenario/input.env" <<EOF
flavor=${flavor}
kernel_release=${kernel_release}
install_method=direct-dpkg
dkc_package_count=10
test_package_count=1
EOF
(
	cd "$scenario"
	sha256sum seed.iso results.img
) >"$scenario/inputs.sha256"

cat >"$evidence/input-preparation.env" <<EOF
status=PASS
flavor=${flavor}
install_method=direct-dpkg
dkc_package_count=10
test_package_count=1
EOF
