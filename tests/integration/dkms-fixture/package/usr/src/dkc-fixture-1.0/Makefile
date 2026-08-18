obj-m += dkc_fixture.o

KERNELRELEASE ?= $(shell uname -r)
KERNEL_DIR := /lib/modules/$(KERNELRELEASE)/build

.PHONY: all clean
all:
	$(MAKE) -C $(KERNEL_DIR) M=$(CURDIR) V=1 modules

clean:
	$(MAKE) -C $(KERNEL_DIR) M=$(CURDIR) clean
