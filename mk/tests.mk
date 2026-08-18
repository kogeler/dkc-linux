##@ Tests

# The fast tier runs offline in a confined container. It is the tier that must
# stay green on every change; everything slower is a separate target.

.PHONY: fast
fast: test typecheck lint lint-make ## Fast tier: unit tests, types, and static analysis

.PHONY: test
test: image ## Unit and fixture tests (container, offline)
	@$(CONTAINER_RUN) --name test -- \
		python3 -m pytest tests -q --no-header

.PHONY: test-verbose
test-verbose: image ## Unit and fixture tests with per-test output
	@$(CONTAINER_RUN) --name test -- \
		python3 -m pytest tests -v --no-header

.PHONY: typecheck
typecheck: image ## Static type check of the typed implementation (container, offline)
	@$(CONTAINER_RUN) --name typecheck -- \
		mypy --strict --no-error-summary dkc
	@echo 'typecheck PASS'

.PHONY: fixtures
fixtures: image ## Refresh test fixtures from the real Debian archive (needs network)
	@$(DKC_ROOT)/scripts/refresh-sources-fixture.sh

.PHONY: release-preflight
release-preflight: build-image ## Verify source, overlay, toolchain, package graph, and dependency closure (needs network)
	@$(CONTAINER_RUN) --net --image '$(BUILD_IMAGE)' --name release-preflight -- \
		scripts/in-container/verify-overlay.sh \
		'$(DKC_DSC_URL)' '$(DKC_DSC_SHA256)' '$(DKC_DSC_SIZE)' \
		'$(DKC_ORIG_TAR_URL)' '$(DKC_ORIG_TAR_SHA256)' '$(DKC_ORIG_TAR_SIZE)' \
		'$(DKC_DEBIAN_TAR_URL)' '$(DKC_DEBIAN_TAR_SHA256)' '$(DKC_DEBIAN_TAR_SIZE)' \
		'$(LLVM_MAJOR)'

.PHONY: overlay-patches
overlay-patches: build-image ## Regenerate the packaging overlay against the current source (needs network)
	@$(CONTAINER_RUN) --net --image '$(BUILD_IMAGE)' --name regen -- \
		scripts/in-container/refresh-overlay-patches.sh \
		'$(DKC_ORIG_TAR_URL)' '$(DKC_ORIG_TAR_SHA256)' \
		'$(DKC_DEBIAN_TAR_URL)' '$(DKC_DEBIAN_TAR_SHA256)' '$(LLVM_MAJOR)' \
		| tar --extract --file=- --directory=$(DKC_ROOT)/debian-overlay/patches --no-same-owner
	@echo 'overlay patches regenerated'

.PHONY: closure-proof
closure-proof: build-image ## Resolve every installed package to an allowed Debian origin (needs network)
	@$(CONTAINER_RUN) --net --image '$(BUILD_IMAGE)' --name closure -- \
		scripts/in-container/closure-proof.sh
