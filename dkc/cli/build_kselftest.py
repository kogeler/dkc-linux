"""Write the selftest build report for build-kselftest.sh."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys


def main() -> None:
    arguments = sys.argv[1:]
    profile = pathlib.Path(arguments[0])
    config = pathlib.Path(arguments[1])
    evidence = pathlib.Path(arguments[2])
    llvm_major = int(arguments[3])
    install_root = pathlib.Path(arguments[4])
    identity_path = pathlib.Path(arguments[5])
    flavor = arguments[6]
    profile_kind = arguments[7]

    def sha256(path: pathlib.Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    targets = (
        (evidence / "kselftest-targets.txt").read_text(encoding="utf-8").splitlines()
    )
    tests = (evidence / "kselftest-tests.txt").read_text(encoding="utf-8").splitlines()
    v3_tests = (
        (evidence / "kselftest-v3-tests.txt").read_text(encoding="utf-8").splitlines()
    )
    runtime = {}
    for line in (
        (install_root / "dkc-profile/runtime.env")
        .read_text(encoding="utf-8")
        .splitlines()
    ):
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
        raise SystemExit(
            "publication identity lacks the selected kselftest build identity"
        )
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


if __name__ == "__main__":
    main()
