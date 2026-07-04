"""vastkit — zero-dependency toolkit to search, rent and drive GPU servers on Vast.ai."""

from __future__ import annotations

from .api import VastAPI, VastAPIError
from .models import Instance, Offer
from .lifecycle import RentSpec, rent, resolve_instance
from .query import build_query, parse_filter

__version__ = "0.1.0"

__all__ = [
    "VastAPI",
    "VastAPIError",
    "Offer",
    "Instance",
    "RentSpec",
    "rent",
    "resolve_instance",
    "build_query",
    "parse_filter",
    "__version__",
]
