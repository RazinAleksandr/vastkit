"""Build Vast.ai search-query bodies from friendly CLI-level options."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ISO 3166 codes for the "EU" geolocation shorthand (EU + EEA + CH/UK).
EU_COUNTRIES = [
    "AT", "BE", "BG", "CH", "CZ", "DE", "DK", "EE", "ES", "FI",
    "FR", "GB", "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV",
    "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
]

_OPS = {"eq", "neq", "gt", "gte", "lt", "lte", "in", "notin"}


def _cast(value: str) -> Any:
    low = value.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value.strip()


def parse_filter(spec: str) -> Dict[str, dict]:
    """Parse an escape-hatch filter ``key=op:value`` into an API fragment.

    Examples::

        cuda_max_good=gte:12.4   -> {"cuda_max_good": {"gte": 12.4}}
        gpu_name=in:L40S,A40     -> {"gpu_name": {"in": ["L40S", "A40"]}}
        datacenter=eq:true       -> {"datacenter": {"eq": True}}
    """
    if "=" not in spec:
        raise ValueError(f"filter must look like key=op:value, got {spec!r}")
    key, rhs = spec.split("=", 1)
    if ":" not in rhs:
        raise ValueError(f"filter must look like key=op:value, got {spec!r}")
    op, raw_val = rhs.split(":", 1)
    op = op.strip().lower()
    if op not in _OPS:
        raise ValueError(f"unknown filter op {op!r} (expected one of {sorted(_OPS)})")
    if op in ("in", "notin"):
        value: Any = [_cast(v) for v in raw_val.split(",") if v.strip()]
    else:
        value = _cast(raw_val)
    return {key.strip(): {op: value}}


def build_query(
    gpus: Optional[List[str]] = None,
    min_vram_gb: float = 0.0,
    min_disk_gb: float = 0.0,
    max_dph: float = 0.0,
    min_inet_down: float = 0.0,
    min_reliability: float = 0.0,
    min_cuda: float = 0.0,
    num_gpus: int = 1,
    extra_filters: Optional[List[str]] = None,
) -> dict:
    """Build the JSON body for ``POST /bundles/``.

    Geolocation is intentionally NOT part of the server-side query — the API's
    geolocation matching is unreliable (values look like ``"Spain, ES"``), so
    filter client-side with :func:`match_geolocation` instead.
    """
    query: Dict[str, Any] = {
        "verified": {"eq": True},
        "rentable": {"eq": True},
        "type": "on-demand",
        "order": [["dph_total", "asc"]],
    }
    if gpus:
        if len(gpus) == 1:
            query["gpu_name"] = {"eq": gpus[0]}
        else:
            query["gpu_name"] = {"in": list(gpus)}
    if num_gpus:
        query["num_gpus"] = {"eq": num_gpus}
    if min_vram_gb:
        query["gpu_ram"] = {"gte": min_vram_gb * 1024.0}  # API uses MB
    if min_disk_gb:
        query["disk_space"] = {"gte": min_disk_gb}
    if max_dph:
        query["dph_total"] = {"lte": max_dph}
    if min_inet_down:
        query["inet_down"] = {"gte": min_inet_down}
    if min_reliability:
        query["reliability2"] = {"gte": min_reliability}
    if min_cuda:
        query["cuda_max_good"] = {"gte": min_cuda}
    for spec in extra_filters or []:
        query.update(parse_filter(spec))
    return query


def expand_geolocation(geo: str) -> List[str]:
    """``"EU"`` -> country list; ``"SE,NO"`` -> ["SE", "NO"]; "" -> []."""
    geo = (geo or "").strip()
    if not geo:
        return []
    if geo.upper() == "EU":
        return list(EU_COUNTRIES)
    return [c.strip().upper() for c in geo.split(",") if c.strip()]


def match_geolocation(offer_geo: str, codes: List[str]) -> bool:
    """Match ``"Spain, ES"`` / ``"ES"`` style geolocation against codes."""
    if not codes:
        return True
    geo = (offer_geo or "").upper()
    return any(geo == code or geo.endswith(f", {code}") for code in codes)
