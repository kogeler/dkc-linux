##@ Kernel build

BUILD_JOBS ?= $(shell nproc)
FLAVOR ?= v2
KERNEL_LTO ?= thin
UPDATE_LATEST ?= 1
REATTEST_MODE ?= full
REATTEST_RESULT ?= $(DKC_ROOT)/out/flavors/$(FLAVOR)/latest
RECOVER_RESULT ?=
MATRIX_V2 ?= $(DKC_ROOT)/out/flavors/v2/latest
MATRIX_V3 ?= $(DKC_ROOT)/out/flavors/v3/latest
APT_REPOSITORY_PHASE ?= all
PACKAGE_MATRIX_RESULT ?= $(DKC_ROOT)/out/package-matrix/latest
APT_UNSIGNED_RESULT ?= $(DKC_ROOT)/out/apt-unsigned/latest
APT_SIGNATURE_RESULT ?= $(DKC_ROOT)/out/apt-signature/latest
APT_KEYS_DIR ?= $(DKC_ROOT)/keys
APT_REPOSITORY_EPOCH ?= $(shell date -u +%s)
APT_REPOSITORY_GENERATION ?= 0
APT_CLOCK_SKEW_SECONDS ?= 86400
APT_SIGNING_SAFETY_SECONDS ?= 2592000
APT_PREVIOUS_POOL_RESULT ?=
APT_PREVIOUS_STATE_RESULT ?=
APT_RETENTION_MODE ?= series-size
APT_RETENTION_MAX_BYTES ?= 9500000000
APT_LIFECYCLE_MODE ?= build
APT_PREVIOUS_STATE_PRESENT ?= false
LIFECYCLE_DECISION_RESULT ?=
KEY_WORKSPACE ?=
STORAGE_REPOSITORY_RESULT ?= $(DKC_ROOT)/out/apt-repository/latest
STORAGE_CONNECTION_FILE ?=
STORAGE_DISPOSABLE_RESULT ?=
STORAGE_STATE_CONNECTION_FILE ?=
SOURCE_DISCOVERY_EPOCH ?= $(shell date -u +%s)
LIFECYCLE_DECISION_EPOCH ?= $(shell date -u +%s)
SOURCE_DISCOVERY_RESULT ?= $(DKC_ROOT)/out/source-discovery/latest
AUTHORITATIVE_STATE_RESULT ?= $(DKC_ROOT)/out/authoritative-state/latest
LIFECYCLE_BOOTSTRAP_ALLOWED ?= 0
STORAGE_PUBLISH_CONNECTION_FILE ?=
STORAGE_PUBLISH_RESULT ?= $(DKC_ROOT)/out/apt-repository/latest
STORAGE_MAX_OBJECT_BYTES ?= 512000000
STORAGE_LEASE_TTL_SECONDS ?= 900
STORAGE_TAKEOVER_GRACE_SECONDS ?= 300
STORAGE_GC_MAX_OBJECTS ?= 10000
STORAGE_GC_MAX_BYTES ?= 10000000000
WORKFLOW_RUN_ID ?= 1
WORKFLOW_RUN_ATTEMPT ?= 1
STORAGE_POOL_CONNECTION_FILE ?=
STORAGE_POOL_STATE_RESULT ?= $(DKC_ROOT)/out/authoritative-state/latest
STORAGE_POOL_OUTPUT_ROOT ?= $(DKC_ROOT)/out/storage-pool
APT_REPOSITORY_IMAGE_PREREQUISITES = $(if $(filter sign,$(APT_REPOSITORY_PHASE)),,image $(if $(filter all verify,$(APT_REPOSITORY_PHASE)),apt-client-image))

.PHONY: source-discover
source-discover: image ## Resolve the newest authenticated Debian kernel source
	@$(DKC_ROOT)/scripts/source-discover.sh \
		'$(TOOLBOX_IMAGE)' '$(SOURCE_DISCOVERY_EPOCH)'

.PHONY: storage-state-read
storage-state-read: image ## Read and verify the authoritative signed repository state
	@$(MAKE) --no-print-directory storage-state-read-prepared

.PHONY: storage-state-read-prepared
storage-state-read-prepared: ## Read state after the toolbox has been prepared
	@$(DKC_ROOT)/scripts/storage-state-read.sh \
		'$(TOOLBOX_IMAGE)' '$(APT_KEYS_DIR)' '$(STORAGE_STATE_CONNECTION_FILE)'

.PHONY: lifecycle-decide
lifecycle-decide: image ## Decide whether the repository must build, refresh, or stop
	@$(DKC_ROOT)/scripts/lifecycle-decide.sh \
		'$(TOOLBOX_IMAGE)' '$(SOURCE_DISCOVERY_RESULT)' \
		'$(AUTHORITATIVE_STATE_RESULT)' '$(LIFECYCLE_DECISION_EPOCH)' \
		'$(LIFECYCLE_BOOTSTRAP_ALLOWED)' '$(DKC_REVISION)' '$(KERNEL_LTO)' \
		'$(APT_RETENTION_MODE)' '$(if $(filter series,$(APT_RETENTION_MODE)),,$(APT_RETENTION_MAX_BYTES))'

.PHONY: storage-publish
storage-publish: image ## Conditionally publish one verified signed repository
	@$(MAKE) --no-print-directory storage-publish-prepared

.PHONY: storage-publish-prepared
storage-publish-prepared: ## Publish after the toolbox has been prepared
	@test -n '$(CANONICAL_REPOSITORY)' -a -n '$(EXPECTED_COMMIT)' || \
		{ echo 'usage: make storage-publish CANONICAL_REPOSITORY=owner/repo EXPECTED_COMMIT=<full-sha>'; exit 1; }
	@$(DKC_ROOT)/scripts/storage-publish.sh \
		'$(TOOLBOX_IMAGE)' '$(STORAGE_PUBLISH_RESULT)' '$(APT_KEYS_DIR)' \
		'$(STORAGE_PUBLISH_CONNECTION_FILE)' '$(CANONICAL_REPOSITORY)' \
		'$(EXPECTED_COMMIT)' '$(WORKFLOW_RUN_ID)' '$(WORKFLOW_RUN_ATTEMPT)' \
		'$(STORAGE_MAX_OBJECT_BYTES)' '$(STORAGE_LEASE_TTL_SECONDS)' \
		'$(STORAGE_TAKEOVER_GRACE_SECONDS)' '$(STORAGE_GC_MAX_OBJECTS)' \
		'$(STORAGE_GC_MAX_BYTES)'

.PHONY: storage-export-pool
storage-export-pool: image ## Export and verify the current immutable pool through S3
	@$(MAKE) --no-print-directory storage-export-pool-prepared

.PHONY: storage-export-pool-prepared
storage-export-pool-prepared: ## Export the live pool after the toolbox has been prepared
	@$(DKC_ROOT)/scripts/storage-export-pool.sh \
		'$(TOOLBOX_IMAGE)' '$(STORAGE_POOL_STATE_RESULT)' \
		'$(STORAGE_POOL_CONNECTION_FILE)' '$(STORAGE_POOL_OUTPUT_ROOT)'

.PHONY: build-flavor
build-flavor: build-image ## Build and attest one DKC flavor, offline after shared-input staging
	@$(DKC_ROOT)/scripts/build-one.sh \
		'$(BUILD_IMAGE)' '$(LLVM_MAJOR)' '$(BUILD_JOBS)' '$(FLAVOR)' \
		'$(DKC_DSC_URL)' '$(DKC_DSC_NAME)' '$(DKC_DSC_SHA256)' '$(DKC_DSC_SIZE)' \
		'$(DKC_ORIG_TAR_URL)' '$(DKC_ORIG_TAR_NAME)' '$(DKC_ORIG_TAR_SHA256)' '$(DKC_ORIG_TAR_SIZE)' \
		'$(DKC_DEBIAN_TAR_URL)' '$(DKC_DEBIAN_TAR_NAME)' '$(DKC_DEBIAN_TAR_SHA256)' '$(DKC_DEBIAN_TAR_SIZE)' \
		'$(DKC_SOURCE_VERSION)' '$(DKC_REVISION)' '$(KERNEL_LTO)' \
		'$(UPDATE_LATEST)'

.PHONY: reattest-flavor
reattest-flavor: build-image ## Replay package, Kbuild, and SIMD attestation without compiling
	@$(DKC_ROOT)/scripts/reattest-flavor.sh \
		'$(BUILD_IMAGE)' '$(LLVM_MAJOR)' '$(FLAVOR)' '$(REATTEST_RESULT)' \
		'$(REATTEST_MODE)' '$(UPDATE_LATEST)'

.PHONY: recover-flavor-export
recover-flavor-export: build-image ## Recover an export after all compilation gates passed
	@test -n '$(RECOVER_RESULT)' || { echo 'usage: make recover-flavor-export FLAVOR=v3 RECOVER_RESULT=/path/to/result'; exit 1; }
	@$(DKC_ROOT)/scripts/recover-flavor-export.sh \
		'$(BUILD_IMAGE)' '$(FLAVOR)' '$(RECOVER_RESULT)' '$(UPDATE_LATEST)'

.PHONY: recover-flavor-attestation
recover-flavor-attestation: build-image ## Replay failed post-build attestations and recover the completed compilation
	@test -n '$(RECOVER_RESULT)' || { echo 'usage: make recover-flavor-attestation FLAVOR=v3 RECOVER_RESULT=/path/to/result'; exit 1; }
	@$(DKC_ROOT)/scripts/recover-flavor-attestation.sh \
		'$(BUILD_IMAGE)' '$(LLVM_MAJOR)' '$(FLAVOR)' '$(RECOVER_RESULT)' \
		'$(UPDATE_LATEST)'

.PHONY: package-matrix
package-matrix: image base-image ## Reconcile and install-test the v2/v3 release packages in clean clients
	@$(DKC_ROOT)/scripts/package-matrix.sh \
		'$(TOOLBOX_IMAGE)' '$(BASE_IMAGE)' '$(LLVM_MAJOR)' \
		'$(MATRIX_V2)' '$(MATRIX_V3)'

.PHONY: package-matrix-verify-lifecycle
package-matrix-verify-lifecycle: ## Verify the bounded package lifecycle handoff
	@$(DKC_ROOT)/scripts/package-matrix-manifest.sh verify-lifecycle \
		'$(PACKAGE_MATRIX_RESULT)'

.PHONY: apt-repository
apt-repository: $(APT_REPOSITORY_IMAGE_PREREQUISITES) ## Assemble, sign, or verify the common binary/source APT repository
	@$(DKC_ROOT)/scripts/apt-repository.sh \
		'$(APT_REPOSITORY_PHASE)' '$(TOOLBOX_IMAGE)' '$(APT_CLIENT_IMAGE)' \
		'$(PACKAGE_MATRIX_RESULT)' '$(APT_UNSIGNED_RESULT)' \
		'$(APT_SIGNATURE_RESULT)' '$(APT_KEYS_DIR)' \
		'$(APT_REPOSITORY_EPOCH)' '$(APT_REPOSITORY_GENERATION)' \
		'$(APT_CLOCK_SKEW_SECONDS)' '$(APT_SIGNING_SAFETY_SECONDS)' \
		'$(APT_PREVIOUS_POOL_RESULT)' '$(APT_PREVIOUS_STATE_RESULT)' \
		'$(APT_RETENTION_MODE)' '$(if $(filter series,$(APT_RETENTION_MODE)),,$(APT_RETENTION_MAX_BYTES))' \
		'$(LIFECYCLE_DECISION_RESULT)'

.PHONY: apt-repository-assemble-lifecycle
apt-repository-assemble-lifecycle: image ## Assemble one unsigned repository from an explicit lifecycle branch
	@$(DKC_ROOT)/scripts/assemble-lifecycle-repository.sh \
		'$(TOOLBOX_IMAGE)' '$(PACKAGE_MATRIX_RESULT)' '$(APT_KEYS_DIR)' \
		'$(APT_REPOSITORY_EPOCH)' '$(APT_REPOSITORY_GENERATION)' \
		'$(APT_PREVIOUS_STATE_PRESENT)' '$(APT_PREVIOUS_POOL_RESULT)' \
		'$(APT_PREVIOUS_STATE_RESULT)' '$(APT_RETENTION_MODE)' \
		'$(if $(filter series,$(APT_RETENTION_MODE)),,$(APT_RETENTION_MAX_BYTES))' \
		'$(APT_LIFECYCLE_MODE)'

.PHONY: apt-repository-sign
apt-repository-sign: image ## Sign one strict unsigned repository handoff
	@$(MAKE) --no-print-directory apt-repository-sign-prepared

.PHONY: apt-repository-sign-prepared
apt-repository-sign-prepared: ## Sign after the toolbox has been prepared
	@$(DKC_ROOT)/scripts/apt-repository.sh \
		sign '$(TOOLBOX_IMAGE)' '$(APT_CLIENT_IMAGE)' \
		'$(PACKAGE_MATRIX_RESULT)' '$(APT_UNSIGNED_RESULT)' \
		'$(APT_SIGNATURE_RESULT)' '$(APT_KEYS_DIR)' \
		'$(APT_REPOSITORY_EPOCH)' '$(APT_REPOSITORY_GENERATION)' \
		'$(APT_CLOCK_SKEW_SECONDS)' '$(APT_SIGNING_SAFETY_SECONDS)' \
		'$(APT_PREVIOUS_POOL_RESULT)' '$(APT_PREVIOUS_STATE_RESULT)' \
		'$(APT_RETENTION_MODE)' '$(if $(filter series,$(APT_RETENTION_MODE)),,$(APT_RETENTION_MAX_BYTES))' \
		'$(LIFECYCLE_DECISION_RESULT)'

.PHONY: apt-repository-verify-decision
apt-repository-verify-decision: image apt-client-image ## Verify a signed repository and bind it to its lifecycle decision
	@test -n '$(LIFECYCLE_DECISION_RESULT)' || { echo 'usage: make apt-repository-verify-decision LIFECYCLE_DECISION_RESULT=/path'; exit 1; }
	@$(DKC_ROOT)/scripts/apt-repository.sh \
		verify '$(TOOLBOX_IMAGE)' '$(APT_CLIENT_IMAGE)' \
		'$(PACKAGE_MATRIX_RESULT)' '$(APT_UNSIGNED_RESULT)' \
		'$(APT_SIGNATURE_RESULT)' '$(APT_KEYS_DIR)' \
		'$(APT_REPOSITORY_EPOCH)' '$(APT_REPOSITORY_GENERATION)' \
		'$(APT_CLOCK_SKEW_SECONDS)' '$(APT_SIGNING_SAFETY_SECONDS)' \
		'$(APT_PREVIOUS_POOL_RESULT)' '$(APT_PREVIOUS_STATE_RESULT)' \
		'$(APT_RETENTION_MODE)' '$(if $(filter series,$(APT_RETENTION_MODE)),,$(APT_RETENTION_MAX_BYTES))' \
		'$(LIFECYCLE_DECISION_RESULT)'
	@$(DKC_ROOT)/scripts/release-gate.py publication-decision \
		--decision '$(LIFECYCLE_DECISION_RESULT)' \
		--repository-result '$(DKC_ROOT)/out/apt-repository/$(DKC_RUN_ID)'

.PHONY: storage-state-require-generation
storage-state-require-generation: ## Require one verified authoritative-state handoff generation
	@test -n '$(EXPECTED_STATE_GENERATION)' || { echo 'usage: make storage-state-require-generation EXPECTED_STATE_GENERATION=N'; exit 1; }
	@$(DKC_ROOT)/scripts/release-gate.py state-generation \
		--state '$(AUTHORITATIVE_STATE_RESULT)' \
		--expected '$(EXPECTED_STATE_GENERATION)' \
		--keyring '$(APT_KEYS_DIR)/dkc-archive-keyring.gpg' \
		--signing-subkeys '$(APT_KEYS_DIR)/archive-signing-subkeys.fingerprints'

.PHONY: archive-key
archive-key: ## Provision the four-year archive certificate on an offline machine
	@test -n '$(KEY_WORKSPACE)' || { echo 'usage: make archive-key KEY_WORKSPACE=/secure/new-directory'; exit 1; }
	@$(DKC_ROOT)/scripts/generate-archive-key.sh '$(KEY_WORKSPACE)'

.PHONY: storage-disposable
storage-disposable: image ## Qualify a verified repository under one disposable storage prefix
	@$(DKC_ROOT)/scripts/storage-disposable.sh \
		'$(TOOLBOX_IMAGE)' '$(STORAGE_REPOSITORY_RESULT)' '$(STORAGE_CONNECTION_FILE)'

.PHONY: storage-disposable-cleanup
storage-disposable-cleanup: image ## Remove an interrupted disposable storage prefix
	@test -n '$(STORAGE_DISPOSABLE_RESULT)' || { echo 'usage: make storage-disposable-cleanup STORAGE_DISPOSABLE_RESULT=/absolute/result STORAGE_CONNECTION_FILE=/secure/storage.json'; exit 1; }
	@test -n '$(STORAGE_CONNECTION_FILE)' || { echo 'usage: make storage-disposable-cleanup STORAGE_DISPOSABLE_RESULT=/absolute/result STORAGE_CONNECTION_FILE=/secure/storage.json'; exit 1; }
	@$(DKC_ROOT)/scripts/storage-disposable-cleanup.sh \
		'$(TOOLBOX_IMAGE)' '$(STORAGE_DISPOSABLE_RESULT)' '$(STORAGE_CONNECTION_FILE)'
