# Debian Kernel Current (DKC) — automation entry point.
#
# Contract:
#   - every repeatable action is a target here or in mk/*.mk;
#   - local/build/production targets require no sudo and do not mutate the host;
#   - every target that creates a resource cleans up only its own, by run ID;
#   - `make help` is the only entry point an operator needs.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -Eeuo pipefail -c
.DEFAULT_GOAL := help
MAKEFLAGS += --no-builtin-rules
.SUFFIXES:

# Absolute path of this Makefile's directory, without the trailing slash.
DKC_ROOT := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
export DKC_ROOT

# One run ID per make invocation. Shared with every script and stamped onto
# every ephemeral container, volume, and VM overlay.
ifndef DKC_RUN_ID
DKC_RUN_ID := $(shell date -u +%Y%m%dT%H%M%SZ)-$(shell od -An -N4 -tx1 /dev/urandom | tr -d ' \n')
endif
export DKC_RUN_ID

DKC_CACHE_DIR ?= $(if $(XDG_CACHE_HOME),$(XDG_CACHE_HOME),$(HOME)/.cache)/dkc
export DKC_CACHE_DIR

EVIDENCE_DIR := $(DKC_ROOT)/evidence

# The source under audit. These come from discovery in normal operation; they
# are spelled out here so the audit target is runnable on its own.
DKC_DEBIAN_POOL ?= http://deb.debian.org/debian/pool/main/l/linux
DKC_DSC_URL ?= $(DKC_DEBIAN_POOL)/linux_7.1.7-1.dsc
DKC_DSC_NAME ?= $(notdir $(DKC_DSC_URL))
DKC_DSC_SHA256 ?= 6bee02071d610626e055dd54130669f29d9de574cc0cacce8c065e07a87afa99
DKC_DSC_SIZE ?= 194732
DKC_ORIG_TAR_URL ?= $(DKC_DEBIAN_POOL)/linux_7.1.7.orig.tar.xz
DKC_ORIG_TAR_NAME ?= $(notdir $(DKC_ORIG_TAR_URL))
DKC_ORIG_TAR_SHA256 ?= cb552bf2695d7080602f829a2911fa21fe62064b96298ce684c62a172277fd87
DKC_ORIG_TAR_SIZE ?= 161674496
DKC_DEBIAN_TAR_URL ?= $(DKC_DEBIAN_POOL)/linux_7.1.7-1.debian.tar.xz
DKC_DEBIAN_TAR_NAME ?= $(notdir $(DKC_DEBIAN_TAR_URL))
DKC_DEBIAN_TAR_SHA256 ?= a871a770847084a29af3e4d51a16b1529f01617e09715365c186b9d1ab2d2b02
DKC_DEBIAN_TAR_SIZE ?= 1523532
DKC_SOURCE_VERSION ?= 7.1.7-1
DKC_REVISION ?= 1

CONTAINER_RUN := $(DKC_ROOT)/scripts/container-run.sh

include $(DKC_ROOT)/mk/help.mk
include $(DKC_ROOT)/mk/preflight.mk
include $(DKC_ROOT)/mk/github.mk
include $(DKC_ROOT)/mk/container.mk
include $(DKC_ROOT)/mk/build.mk
include $(DKC_ROOT)/mk/vm.mk
include $(DKC_ROOT)/mk/quality.mk
include $(DKC_ROOT)/mk/tests.mk
include $(DKC_ROOT)/mk/housekeeping.mk
