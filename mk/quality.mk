##@ Quality

SHELL_SOURCES = $(shell git -C $(DKC_ROOT) ls-files --cached --others --exclude-standard -- '*.sh' 2>/dev/null | \
	while IFS= read -r path; do test -f '$(DKC_ROOT)/'"$$path" && printf '%s ' "$$path"; done)

.PHONY: lint
lint: lint-shell lint-language ## Run every static check (container tier, offline)

.PHONY: lint-language
lint-language: ## Everything committed must be written in English
	@# Project documentation and source are English-only. Cyrillic, Greek, CJK
	@# and Hebrew ranges are checked as a compact fail-closed policy.
	@bad=$$(git -C $(DKC_ROOT) grep -I -lnP '[\x{0400}-\x{04FF}\x{0370}-\x{03FF}\x{4E00}-\x{9FFF}\x{3040}-\x{30FF}\x{0590}-\x{05FF}]' -- . 2>/dev/null || true); \
	if [ -n "$$bad" ]; then \
		echo 'non-English text in tracked files:'; \
		for f in $$bad; do echo "  $$f"; done; \
		exit 1; \
	fi
	@msgs=$$(git -C $(DKC_ROOT) log --format='%H %s%n%b' 2>/dev/null \
		| grep -cP '[\x{0400}-\x{04FF}\x{0370}-\x{03FF}\x{4E00}-\x{9FFF}\x{3040}-\x{30FF}\x{0590}-\x{05FF}]' || true); \
	if [ "$$msgs" != "0" ]; then echo "non-English text in commit messages: $$msgs line(s)"; exit 1; fi
	@echo 'lint-language PASS'

.PHONY: lint-shell
lint-shell: image ## ShellCheck and shfmt over all project shell sources
	@test -n '$(SHELL_SOURCES)' || { echo 'no project shell sources yet'; exit 0; }
	@$(CONTAINER_RUN) --name lint -- bash -c \
		'set -Eeuo pipefail; \
		 echo "shellcheck:"; shellcheck --external-sources --severity=style --shell=bash $(SHELL_SOURCES); \
		 echo "shfmt:"; shfmt --diff --language-dialect bash $(SHELL_SOURCES)'
	@echo 'lint PASS'

.PHONY: fmt
fmt: image ## Format shell sources in place (container tier, offline)
	@test -n '$(SHELL_SOURCES)' || { echo 'no project shell sources yet'; exit 0; }
	@# The container cannot write to the repository, so the formatted files come
	@# back as a tar on stdout and are unpacked over exactly the same paths.
	@$(CONTAINER_RUN) --name fmt -- bash -c \
		'set -Eeuo pipefail; \
		 shfmt --write --language-dialect bash $(SHELL_SOURCES) >&2; \
		 tar --create --file=- $(SHELL_SOURCES)' \
		| tar --extract --file=- --directory=$(DKC_ROOT) --no-same-owner $(SHELL_SOURCES)
	@echo 'formatted'

.PHONY: lint-make
lint-make: ## Check that every make target carries a help description
	@bad=$$(awk '/^[a-zA-Z0-9_-]+:([^=]|$$)/ && !/##/ && !/^\.PHONY/ {print FILENAME": "$$1}' \
		$(DKC_ROOT)/Makefile $(DKC_ROOT)/mk/*.mk); \
	if [ -n "$$bad" ]; then echo "targets without a ## description:"; echo "$$bad"; exit 1; fi
	@echo 'lint-make PASS'
