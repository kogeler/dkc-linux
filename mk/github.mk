##@ GitHub workflow adapters

GITHUB_RUN_ROLE ?=
GITHUB_IMAGE_RESOLVE_TIMEOUT ?= 10800
GITHUB_CANONICAL_REPOSITORY ?= kogeler/dkc-linux
GITHUB_QEMU_FLAVOR ?=
GITHUB_LIFECYCLE_RESULT ?= $(DKC_ROOT)/out/lifecycle-decision/$(DKC_RUN_ID)
GITHUB_SOURCE_RESULT ?= $(DKC_ROOT)/out/source-discovery/$(DKC_RUN_ID)
GITHUB_RELEASE_CACHE_ROOT ?= $(DKC_ROOT)/out/release-cache/$(FLAVOR)
GITHUB_RELEASE_CACHE_KEY ?=
GITHUB_RELEASE_CACHE_FLAVOR_RESULT ?= $(DKC_ROOT)/out/flavors/$(FLAVOR)/$(DKC_RUN_ID)
GITHUB_RELEASE_CACHE_SELFTEST_RESULT ?= $(DKC_ROOT)/out/kselftest/qualification/$(FLAVOR)/$(DKC_RUN_ID)
GITHUB_RELEASE_CACHE_QEMU_RESULT ?= $(DKC_ROOT)/out/qemu-boot/$(DKC_RUN_ID)
GITHUB_RELEASE_CACHE_KEY_V2 ?=
GITHUB_RELEASE_CACHE_KEY_V3 ?=

.PHONY: github-lifecycle-gate
github-lifecycle-gate: ## Authorize one exact-main production lifecycle trigger
	@GITHUB_CANONICAL_REPOSITORY='$(GITHUB_CANONICAL_REPOSITORY)' \
		$(DKC_ROOT)/scripts/github-ci.py lifecycle-gate

.PHONY: github-run-id
github-run-id: ## Idempotently assign a role-bound run identity to one GitHub job
	@test -n '$(GITHUB_RUN_ROLE)' || { echo 'usage: make github-run-id GITHUB_RUN_ROLE=<role>'; exit 1; }
	@$(DKC_ROOT)/scripts/github-ci.py run-identity --role '$(GITHUB_RUN_ROLE)'

.PHONY: github-container-images-resolve
github-container-images-resolve: ## Snapshot the current published image bundle for one CI run
	@$(DKC_ROOT)/scripts/github-container-images-resolve.sh \
		'$(GITHUB_EVENT_NAME)' '$(GITHUB_IMAGE_RESOLVE_TIMEOUT)'

.PHONY: github-source-build-env
github-source-build-env: ## Export one authenticated source handoff to the job environment
	@$(DKC_ROOT)/scripts/github-ci.py export-source \
		--source '$(GITHUB_SOURCE_RESULT)'

.PHONY: github-kvm-prepare
github-kvm-prepare: ## Install QEMU and require the selected flavor through KVM
	@test -n '$(GITHUB_QEMU_FLAVOR)' || { echo 'usage: make github-kvm-prepare GITHUB_QEMU_FLAVOR=v2'; exit 1; }
	@$(DKC_ROOT)/scripts/github-prepare-kvm.sh \
		'$(QEMU_CPU_CONFIG)' '$(GITHUB_QEMU_FLAVOR)'

# Secret-bearing jobs prepare and verify the registry image in a separate step.
# These adapters keep image resolution outside the secret-bearing process while
# reusing the ordinary local targets for the actual operation.
.PHONY: github-storage-state-read
github-storage-state-read: storage-state-read-prepared ## Read state with an already prepared toolbox

.PHONY: github-storage-export-pool
github-storage-export-pool: storage-export-pool-prepared ## Export the live pool with an already prepared toolbox

.PHONY: github-storage-publish
github-storage-publish: storage-publish-prepared ## Publish with an already prepared toolbox

.PHONY: github-apt-repository-sign
github-apt-repository-sign: ## Sign an authorized lifecycle handoff with a prepared toolbox
	@test -n '$(LIFECYCLE_DECISION_RESULT)' || \
		{ echo 'LIFECYCLE_DECISION_RESULT is required'; exit 1; }
	@$(MAKE) --no-print-directory apt-repository-sign-prepared

.PHONY: github-apt-repository-qualify
github-apt-repository-qualify: image apt-client-image ## Exercise the complete repository flow with a disposable key
	@DKC_APT_EPHEMERAL_SIGNING=1 $(DKC_ROOT)/scripts/apt-repository.sh \
		qualify '$(TOOLBOX_IMAGE)' '$(APT_CLIENT_IMAGE)' \
		'$(PACKAGE_MATRIX_RESULT)' '$(APT_UNSIGNED_RESULT)' \
		'$(APT_SIGNATURE_RESULT)' '$(APT_KEYS_DIR)' \
		'$(APT_REPOSITORY_EPOCH)' '$(APT_REPOSITORY_GENERATION)' \
		'$(APT_CLOCK_SKEW_SECONDS)' '$(APT_SIGNING_SAFETY_SECONDS)' \
		'' '' '$(APT_RETENTION_MODE)' \
		'$(if $(filter series,$(APT_RETENTION_MODE)),,$(APT_RETENTION_MAX_BYTES))' ''

.PHONY: github-pull-request-qualification
github-pull-request-qualification: ## Create a non-publishing build decision from authenticated source
	@$(DKC_ROOT)/scripts/github-ci.py qualification-decision \
		--source '$(SOURCE_DISCOVERY_RESULT)' \
		--decision '$(GITHUB_LIFECYCLE_RESULT)' --root '$(DKC_ROOT)' \
		--epoch '$(LIFECYCLE_DECISION_EPOCH)' --dkc-revision '$(DKC_REVISION)' \
		--lto-mode '$(KERNEL_LTO)' --retention-mode '$(APT_RETENTION_MODE)' \
		--retention-max-bytes '$(if $(filter series,$(APT_RETENTION_MODE)),,$(APT_RETENTION_MAX_BYTES))'

.PHONY: github-lifecycle-outputs
github-lifecycle-outputs: ## Export typed decision and isolated transport-cache outputs
	@$(DKC_ROOT)/scripts/github-ci.py export-lifecycle \
		--decision '$(GITHUB_LIFECYCLE_RESULT)' --root '$(DKC_ROOT)'

.PHONY: github-release-cache-prepare
github-release-cache-prepare: ## Seal one fully qualified flavor as a content-addressed handoff
	@test -n '$(GITHUB_RELEASE_CACHE_KEY)' || { echo 'GITHUB_RELEASE_CACHE_KEY is required'; exit 1; }
	@$(DKC_ROOT)/scripts/github-ci.py release-cache-prepare \
		--cache '$(GITHUB_RELEASE_CACHE_ROOT)' \
		--flavor-result '$(GITHUB_RELEASE_CACHE_FLAVOR_RESULT)' \
		--selftest-result '$(GITHUB_RELEASE_CACHE_SELFTEST_RESULT)' \
		--qemu-result '$(GITHUB_RELEASE_CACHE_QEMU_RESULT)' \
		--decision '$(GITHUB_LIFECYCLE_RESULT)' --flavor '$(FLAVOR)' \
		--build-image '$(BUILD_IMAGE)' --toolbox-image '$(TOOLBOX_IMAGE)' \
		--key '$(GITHUB_RELEASE_CACHE_KEY)' \
		--root '$(DKC_ROOT)'

.PHONY: github-release-cache-verify
github-release-cache-verify: ## Verify one restored accepted flavor before any consumer uses it
	@test -n '$(GITHUB_RELEASE_CACHE_KEY)' || { echo 'GITHUB_RELEASE_CACHE_KEY is required'; exit 1; }
	@$(DKC_ROOT)/scripts/github-ci.py release-cache-verify \
		--cache '$(GITHUB_RELEASE_CACHE_ROOT)' \
		--decision '$(GITHUB_LIFECYCLE_RESULT)' --flavor '$(FLAVOR)' \
		--key '$(GITHUB_RELEASE_CACHE_KEY)' \
		--root '$(DKC_ROOT)'

.PHONY: github-release-cache-delete
github-release-cache-delete: ## Delete exact main-branch release caches after remote verification
	@test -n '$(GITHUB_RELEASE_CACHE_KEY_V2)' -a -n '$(GITHUB_RELEASE_CACHE_KEY_V3)' || \
		{ echo 'both release-cache keys are required'; exit 1; }
	@$(DKC_ROOT)/scripts/github-ci.py release-cache-delete \
		--v2-key '$(GITHUB_RELEASE_CACHE_KEY_V2)' \
		--v3-key '$(GITHUB_RELEASE_CACHE_KEY_V3)'

.PHONY: github-lifecycle-result
github-lifecycle-result: ## Require the workflow terminal state selected by discovery
	@$(DKC_ROOT)/scripts/github-ci.py terminal-result \
		--decision '$(GITHUB_LIFECYCLE_DECISION)' \
		--decision-result '$(GITHUB_LIFECYCLE_DECISION_RESULT)' \
		--final-result '$(GITHUB_FINAL_STATE_RESULT)'
