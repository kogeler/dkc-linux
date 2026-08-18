##@ Preflight

CANONICAL_REPOSITORY ?=
EXPECTED_COMMIT ?=

.PHONY: doctor
doctor: ## Probe the host for every capability later phases need (read-only)
	@$(DKC_ROOT)/scripts/host-doctor.sh

.PHONY: doctor-json
doctor-json: ## Print the newest preflight evidence record
	@cat $(EVIDENCE_DIR)/preflight/latest.json

.PHONY: preflight
preflight: doctor image ## Host probe plus toolbox image build
	@printf '\npreflight complete\n'

.PHONY: current-main
current-main: ## Require an expected commit to remain canonical main
	@test -n '$(CANONICAL_REPOSITORY)' -a -n '$(EXPECTED_COMMIT)' || \
		{ echo 'usage: make current-main CANONICAL_REPOSITORY=owner/repo EXPECTED_COMMIT=<full-sha>'; exit 1; }
	@$(DKC_ROOT)/scripts/check-current-main.sh \
		'$(CANONICAL_REPOSITORY)' '$(EXPECTED_COMMIT)'
