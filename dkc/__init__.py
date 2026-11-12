"""Debian Kernel Current: typed implementation of identity, discovery, and state.

Shell drives the workflow; anything that parses, compares, hashes, or decides
lives here where it can be typed and tested. Command-line entry points are the
modules of `dkc.cli`.
"""

__all__ = ["buildid", "cli", "debver", "naming", "serialize", "sources"]
