##@ Housekeeping

# Cleanup is deliberately narrow. Broad pruning is forbidden: every removal
# here is scoped to a path inside the repository, or to a resource whose
# dkc.run-id label was verified first.

.PHONY: clean
clean: ## Remove this repository's run scratch directories
	@rm -rf -- '$(DKC_ROOT)/.dkc-run'
	@echo 'removed .dkc-run'

.PHONY: clean-containers
clean-containers: ## Remove only containers labelled as owned by DKC runs
	@$(DKC_ROOT)/scripts/clean-containers.sh

.PHONY: clean-evidence
clean-evidence: ## Remove locally generated evidence records (never published evidence)
	@rm -rf -- '$(EVIDENCE_DIR)'
	@echo 'removed evidence/'

.PHONY: clean-all
clean-all: clean clean-containers ## Scratch plus DKC-labelled containers
	@echo 'clean-all done; the toolbox image and caches are kept on purpose'
