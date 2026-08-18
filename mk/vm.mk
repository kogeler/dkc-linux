##@ Virtual-machine validation

QEMU_IMAGE_CONFIG ?= $(DKC_ROOT)/config/qemu-image.env
QEMU_CPU_CONFIG ?= $(DKC_ROOT)/config/qemu-cpus.env
QEMU_ACCEL ?= kvm
QEMU_PREFLIGHT_FLAVOR ?= all
QEMU_TIMEOUT_SECONDS ?= 2400
QEMU_MEMORY_MIB ?= 4096
QEMU_CPUS ?= 2
FLAVOR_RESULT ?= $(DKC_ROOT)/out/flavors/$(FLAVOR)/latest
KSELFTEST_PROFILE ?= $(DKC_ROOT)/config/kselftest.env
KSELFTEST_KIND ?= qualification
KSELFTEST_RESULT ?= $(DKC_ROOT)/out/kselftest/$(KSELFTEST_KIND)/$(FLAVOR)/latest

.PHONY: kselftest-flavor
kselftest-flavor: build-image ## Build an exact-source selftest bundle without rebuilding kernel packages
	@$(DKC_ROOT)/scripts/build-kselftest-flavor.sh \
		'$(BUILD_IMAGE)' '$(LLVM_MAJOR)' '$(FLAVOR)' '$(FLAVOR_RESULT)' \
		'$(KSELFTEST_PROFILE)' '$(KSELFTEST_KIND)' '$(UPDATE_LATEST)'

.PHONY: vm-base-image
vm-base-image: ## Fetch and checksum-verify the immutable Debian 13 cloud image
	@$(DKC_ROOT)/scripts/fetch-qemu-image.sh '$(QEMU_IMAGE_CONFIG)'

.PHONY: qemu-preflight
qemu-preflight: ## Require KVM support for the configured flavor CPU model(s)
	@$(DKC_ROOT)/scripts/qemu-preflight.sh \
		'$(QEMU_CPU_CONFIG)' '$(QEMU_ACCEL)' '$(QEMU_PREFLIGHT_FLAVOR)'

.PHONY: qemu-boot-flavor
qemu-boot-flavor: image vm-base-image ## Boot and validate one accepted flavor with an external selftest bundle
	@$(DKC_ROOT)/scripts/qemu-boot.sh \
		'$(TOOLBOX_IMAGE)' '$(QEMU_IMAGE_CONFIG)' '$(QEMU_CPU_CONFIG)' \
		'$(FLAVOR)' '$(QEMU_ACCEL)' '$(QEMU_TIMEOUT_SECONDS)' \
		'$(QEMU_MEMORY_MIB)' '$(QEMU_CPUS)' '$(FLAVOR_RESULT)' \
		'$(KSELFTEST_RESULT)' '$(UPDATE_LATEST)'
