"""Shared object metadata for repository storage backends."""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["ObjectMetadata"]


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
@dataclass(frozen=True)
class ObjectMetadata:
    content_type: str
    cache_control: str

    def __post_init__(self) -> None:
        if not self.content_type or _CONTROL_RE.search(self.content_type):
            raise ValueError("content type must be non-empty and contain no controls")
        if not self.cache_control or _CONTROL_RE.search(self.cache_control):
            raise ValueError("cache control must be non-empty and contain no controls")
