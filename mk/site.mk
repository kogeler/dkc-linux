##@ Documentation site

SITE_SOURCE_REVISION ?= $(shell git -C $(DKC_ROOT) rev-parse HEAD 2>/dev/null)
SITE_SOURCE_EPOCH ?= $(shell git -C $(DKC_ROOT) log -1 --format=%ct 2>/dev/null)
SITE_RESULT ?= $(DKC_ROOT)/out/site/latest
SITE_PUBLISH_CONNECTION_FILE ?=
SITE_LEASE_WAIT_SECONDS ?= 1800
SITE_MAX_DELETIONS ?= 400
SITE_PUBLICATION_RESULT ?= $(DKC_ROOT)/out/site-publication/latest
SITE_INDEXNOW_WAIT_SECONDS ?= 600
SITE_INDEXNOW_ENDPOINT ?= https://api.indexnow.org/indexnow

.PHONY: site
site: image ## Build and audit the public documentation site (container, offline)
	@$(DKC_ROOT)/scripts/site-build.sh \
		'$(TOOLBOX_IMAGE)' '$(SITE_SOURCE_REVISION)' '$(SITE_SOURCE_EPOCH)'

.PHONY: site-publish
site-publish: image ## Publish one audited site build below the site/ storage prefix
	@$(MAKE) --no-print-directory site-publish-prepared

.PHONY: site-publish-prepared
site-publish-prepared: ## Publish the site after the toolbox has been prepared
	@test -n '$(CANONICAL_REPOSITORY)' -a -n '$(EXPECTED_COMMIT)' || \
		{ echo 'usage: make site-publish CANONICAL_REPOSITORY=owner/name EXPECTED_COMMIT=<sha> [SITE_PUBLISH_CONNECTION_FILE=/secure/storage.json]'; exit 1; }
	@$(DKC_ROOT)/scripts/site-publish.sh \
		'$(TOOLBOX_IMAGE)' '$(SITE_RESULT)' '$(SITE_PUBLISH_CONNECTION_FILE)' \
		'$(CANONICAL_REPOSITORY)' '$(EXPECTED_COMMIT)' \
		'$(WORKFLOW_RUN_ID)' '$(WORKFLOW_RUN_ATTEMPT)' \
		'$(STORAGE_LEASE_TTL_SECONDS)' '$(STORAGE_TAKEOVER_GRACE_SECONDS)' \
		'$(SITE_LEASE_WAIT_SECONDS)' '$(SITE_MAX_DELETIONS)'

.PHONY: site-indexnow
site-indexnow: ## Submit the pages one site publication changed to IndexNow (network)
	@$(DKC_CLI) dkc.cli.site_indexnow \
		--repository '$(DKC_ROOT)' \
		--site-result '$(SITE_RESULT)' \
		--publication-result '$(SITE_PUBLICATION_RESULT)' \
		--wait-seconds '$(SITE_INDEXNOW_WAIT_SECONDS)' \
		--endpoint '$(SITE_INDEXNOW_ENDPOINT)'
