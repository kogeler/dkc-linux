##@ Container tier

BASE_IMAGE := $(shell cat $(DKC_ROOT)/config/base-image.lock 2>/dev/null)

# Kernel toolchain selection. LLVM_MAJOR names the clang-<major> packages and
# the LLVM=-<major> Kbuild selection. It must stay a compiler Debian ships,
# because the generated headers package depends on it at runtime and every DKMS
# user has to be able to install it.
LLVM_MAJOR ?= 21

DKC_IMAGE_MODE ?= build
TOOLBOX_IMAGE ?= localhost/dkc-toolbox:latest
BUILD_IMAGE ?= localhost/dkc-build:llvm$(LLVM_MAJOR)
APT_CLIENT_IMAGE ?= localhost/dkc-apt-client:latest
DKC_TOOLBOX_LATEST ?= ghcr.io/kogeler/dkc-toolbox:latest
DKC_BUILD_LATEST ?= ghcr.io/kogeler/dkc-kernel-build:latest
DKC_APT_CLIENT_LATEST ?= ghcr.io/kogeler/dkc-apt-client:latest
DKC_IMAGE_HELPER := $(DKC_ROOT)/scripts/container-images.sh
ifndef DKC_IMAGE_BUNDLE_INPUT_SHA256
DKC_IMAGE_BUNDLE_INPUT_SHA256 = $(shell $(DKC_IMAGE_HELPER) fingerprint '$(BASE_IMAGE)' '$(LLVM_MAJOR)')
endif
ifndef DKC_IMAGE_SOURCE_REVISION
DKC_IMAGE_SOURCE_REVISION = $(shell git -C $(DKC_ROOT) rev-parse HEAD 2>/dev/null)
endif
ifndef DKC_IMAGE_BUNDLE_GENERATION
DKC_IMAGE_BUNDLE_GENERATION = local-$(DKC_IMAGE_BUNDLE_INPUT_SHA256)
endif
DKC_IMAGE_RESOLVE_TIMEOUT ?= 3600
DKC_IMAGE_RESOLVE_INTERVAL ?= 30
DKC_IMAGE_RESOLVE_OUTPUT ?= /dev/stdout
DKC_IMAGE_EXPECTED_GENERATION ?=
export DKC_TOOLBOX_IMAGE = $(TOOLBOX_IMAGE)

ifneq ($(words $(DKC_IMAGE_MODE)),1)
$(error DKC_IMAGE_MODE must be exactly build or registry)
endif
ifeq ($(filter $(DKC_IMAGE_MODE),build registry),)
$(error DKC_IMAGE_MODE must be exactly build or registry)
endif

.PHONY: image
ifeq ($(DKC_IMAGE_MODE),build)
image: ## Build or fetch and verify the toolbox image
	@$(DKC_IMAGE_HELPER) build toolbox '$(TOOLBOX_IMAGE)' '$(BASE_IMAGE)' \
		'$(LLVM_MAJOR)' '$(DKC_IMAGE_BUNDLE_INPUT_SHA256)' \
		'$(DKC_IMAGE_BUNDLE_GENERATION)' '$(DKC_IMAGE_SOURCE_REVISION)'
else
image: ## Build or fetch and verify the toolbox image
	@$(DKC_IMAGE_HELPER) ensure toolbox '$(TOOLBOX_IMAGE)'
endif

.PHONY: apt-client-image
ifeq ($(DKC_IMAGE_MODE),build)
apt-client-image: ## Build or fetch and verify the minimal APT client image
	@$(DKC_IMAGE_HELPER) build apt-client '$(APT_CLIENT_IMAGE)' '$(BASE_IMAGE)' \
		'$(LLVM_MAJOR)' '$(DKC_IMAGE_BUNDLE_INPUT_SHA256)' \
		'$(DKC_IMAGE_BUNDLE_GENERATION)' '$(DKC_IMAGE_SOURCE_REVISION)'
else
apt-client-image: ## Build or fetch and verify the minimal APT client image
	@$(DKC_IMAGE_HELPER) ensure apt-client '$(APT_CLIENT_IMAGE)'
endif

.PHONY: build-image
ifeq ($(DKC_IMAGE_MODE),build)
build-image: ## Build or fetch and verify the kernel build image
	@$(DKC_IMAGE_HELPER) build kernel-build '$(BUILD_IMAGE)' '$(BASE_IMAGE)' \
		'$(LLVM_MAJOR)' '$(DKC_IMAGE_BUNDLE_INPUT_SHA256)' \
		'$(DKC_IMAGE_BUNDLE_GENERATION)' '$(DKC_IMAGE_SOURCE_REVISION)'
else
build-image: ## Build or fetch and verify the kernel build image
	@$(DKC_IMAGE_HELPER) ensure kernel-build '$(BUILD_IMAGE)'
endif

.PHONY: base-image
base-image: ## Ensure the digest-pinned Debian base image is locally available
	@$(DKC_IMAGE_HELPER) ensure-base '$(BASE_IMAGE)'

.PHONY: container-images
container-images: image build-image apt-client-image ## Build and verify the complete three-image bundle

.PHONY: container-images-push
container-images-push: ## Push one verified bundle to the three canonical latest tags
	@$(DKC_IMAGE_HELPER) push-bundle \
		'$(TOOLBOX_IMAGE)' '$(BUILD_IMAGE)' '$(APT_CLIENT_IMAGE)' \
		'$(BASE_IMAGE)' '$(LLVM_MAJOR)' '$(DKC_IMAGE_BUNDLE_INPUT_SHA256)' \
		'$(DKC_IMAGE_BUNDLE_GENERATION)' '$(DKC_TOOLBOX_LATEST)' \
		'$(DKC_BUILD_LATEST)' '$(DKC_APT_CLIENT_LATEST)'

.PHONY: container-images-resolve
container-images-resolve: ## Resolve one coherent public latest bundle to immutable digests
	@$(DKC_IMAGE_HELPER) resolve \
		'$(DKC_IMAGE_EXPECTED_GENERATION)' '$(DKC_IMAGE_RESOLVE_TIMEOUT)' \
		'$(DKC_IMAGE_RESOLVE_INTERVAL)' '$(DKC_IMAGE_RESOLVE_OUTPUT)' \
		'$(DKC_TOOLBOX_LATEST)' '$(DKC_BUILD_LATEST)' '$(DKC_APT_CLIENT_LATEST)'

.PHONY: image-digest
image-digest: ## Show the pinned base image and locally available toolbox image ID
	@printf 'base    %s\n' '$(BASE_IMAGE)'
	@podman image inspect '$(TOOLBOX_IMAGE)' --format 'toolbox {{.Id}}' 2>/dev/null \
		|| echo 'toolbox not available yet; run: make image'

.PHONY: shell
shell: image ## Interactive shell in an ephemeral toolbox container (repo at /work/src)
	@$(CONTAINER_RUN) --profile debug --name shell -- bash

.PHONY: shell-net
shell-net: image ## Same as shell, but with networking enabled
	@$(CONTAINER_RUN) --profile debug --net --name shell -- bash

.PHONY: run
run: image ## Run CMD inside an ephemeral container, e.g. make run CMD='python3 -V'
	@test -n '$(CMD)' || { echo 'usage: make run CMD=...'; exit 1; }
	@$(CONTAINER_RUN) --name run -- bash -lc '$(CMD)'
