# Self-documenting help.
#
# Targets document themselves with `## description` on the target line and
# group themselves with `##@ Group name` headings. Nothing else needs to be
# maintained for `make help` to stay accurate.

.PHONY: help
help: ## Show this help
	@printf '\nDebian Kernel Current (DKC) — make targets\n\n'
	@awk 'BEGIN {FS = ":.*##"} \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5); next } \
		/^[a-zA-Z0-9_-]+:.*?##/ && !seen[$$1]++ { \
			printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2 \
		}' \
		$(MAKEFILE_LIST)
	@printf '\nRun ID for this invocation: %s\n\n' '$(DKC_RUN_ID)'

.PHONY: print-run-id
print-run-id: ## Print this invocation's run ID
	@printf '%s\n' '$(DKC_RUN_ID)'

.PHONY: print-config
print-config: ## Print the resolved automation configuration
	@printf 'DKC_ROOT       %s\n' '$(DKC_ROOT)'
	@printf 'DKC_RUN_ID     %s\n' '$(DKC_RUN_ID)'
	@printf 'DKC_CACHE_DIR  %s\n' '$(DKC_CACHE_DIR)'
	@printf 'BASE_IMAGE     %s\n' '$(BASE_IMAGE)'
	@printf 'DKC_IMAGE_MODE %s\n' '$(DKC_IMAGE_MODE)'
	@printf 'TOOLBOX_IMAGE  %s\n' '$(TOOLBOX_IMAGE)'
	@printf 'BUILD_IMAGE    %s\n' '$(BUILD_IMAGE)'
	@printf 'APT_CLIENT_IMAGE %s\n' '$(APT_CLIENT_IMAGE)'
	@printf 'IMAGE_BUNDLE_INPUT %s\n' '$(DKC_IMAGE_BUNDLE_INPUT_SHA256)'
	@printf 'IMAGE_BUNDLE_GENERATION %s\n' '$(DKC_IMAGE_BUNDLE_GENERATION)'
	@printf 'EVIDENCE_DIR   %s\n' '$(EVIDENCE_DIR)'
	@printf 'KERNEL_LTO     %s\n' '$(KERNEL_LTO)'
	@printf 'REATTEST_MODE  %s\n' '$(REATTEST_MODE)'
	@printf 'UPDATE_LATEST  %s\n' '$(UPDATE_LATEST)'
