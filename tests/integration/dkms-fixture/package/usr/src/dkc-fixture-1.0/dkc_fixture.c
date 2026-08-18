// SPDX-License-Identifier: MIT
#include <linux/init.h>
#include <linux/module.h>

static int __init dkc_fixture_init(void)
{
	return 0;
}

static void __exit dkc_fixture_exit(void)
{
}

module_init(dkc_fixture_init);
module_exit(dkc_fixture_exit);

MODULE_DESCRIPTION("Controlled DKC DKMS validation fixture");
MODULE_LICENSE("MIT");
