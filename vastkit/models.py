"""Typed views over Vast.ai API payloads.

Only the fields vastkit actually uses are promoted to attributes; the full
payload is always kept in ``raw`` so nothing is ever lost.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


def _f(d: dict, key: str, default: float = 0.0) -> float:
    v = d.get(key)
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(d: dict, key: str, default: int = 0) -> int:
    v = d.get(key)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@dataclass
class Offer:
    """A rentable machine offer returned by the search endpoint."""

    id: int
    gpu_name: str = ""
    num_gpus: int = 1
    gpu_ram: float = 0.0  # MB, per GPU
    disk_space: float = 0.0  # GB available
    dph_total: float = 0.0  # $/hr for GPU + base disk
    storage_cost: float = 0.0  # $/GB/month
    inet_down: float = 0.0  # Mbps
    inet_up: float = 0.0  # Mbps
    inet_down_cost_per_tb: float = 0.0  # $/TB downloaded
    inet_up_cost_per_tb: float = 0.0
    reliability: float = 0.0  # 0..1
    cuda_max_good: float = 0.0  # max CUDA version the host driver supports
    dlperf: float = 0.0  # vast's deep-learning perf score
    dlperf_per_dphtotal: float = 0.0
    geolocation: str = ""
    verified: bool = False
    machine_id: int = 0
    host_id: int = 0
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_raw(cls, d: dict) -> "Offer":
        return cls(
            id=_i(d, "id"),
            gpu_name=str(d.get("gpu_name") or ""),
            num_gpus=_i(d, "num_gpus", 1),
            gpu_ram=_f(d, "gpu_ram"),
            disk_space=_f(d, "disk_space"),
            dph_total=_f(d, "dph_total"),
            storage_cost=_f(d, "storage_cost"),
            inet_down=_f(d, "inet_down"),
            inet_up=_f(d, "inet_up"),
            inet_down_cost_per_tb=_f(d, "internet_down_cost_per_tb"),
            inet_up_cost_per_tb=_f(d, "internet_up_cost_per_tb"),
            reliability=_f(d, "reliability2", _f(d, "reliability")),
            cuda_max_good=_f(d, "cuda_max_good"),
            dlperf=_f(d, "dlperf"),
            dlperf_per_dphtotal=_f(d, "dlperf_per_dphtotal"),
            geolocation=str(d.get("geolocation") or ""),
            verified=bool(d.get("verified", False)),
            machine_id=_i(d, "machine_id"),
            host_id=_i(d, "host_id"),
            raw=d,
        )

    @property
    def gpu_ram_gb(self) -> float:
        return self.gpu_ram / 1024.0

    @property
    def bw_cost_per_gb(self) -> float:
        """Download bandwidth cost in $/GB."""
        return self.inet_down_cost_per_tb / 1000.0

    def session_cost(self, hours: float = 1.0, download_gb: float = 0.0) -> float:
        """Estimated total cost of a session: rent time + one-time download."""
        return self.dph_total * hours + download_gb * self.bw_cost_per_gb


@dataclass
class Instance:
    """A rented instance (contract)."""

    id: int
    actual_status: str = ""
    intended_status: str = ""
    ssh_host: str = ""
    ssh_port: int = 0
    dph_total: float = 0.0
    label: str = ""
    gpu_name: str = ""
    num_gpus: int = 1
    image: str = ""
    start_date: float = 0.0  # epoch seconds
    public_ipaddr: str = ""
    geolocation: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_raw(cls, d: dict) -> "Instance":
        return cls(
            id=_i(d, "id"),
            actual_status=str(d.get("actual_status") or ""),
            intended_status=str(d.get("intended_status") or ""),
            ssh_host=str(d.get("ssh_host") or ""),
            ssh_port=_i(d, "ssh_port"),
            dph_total=_f(d, "dph_total"),
            label=str(d.get("label") or ""),
            gpu_name=str(d.get("gpu_name") or ""),
            num_gpus=_i(d, "num_gpus", 1),
            image=str(d.get("image_uuid") or d.get("image") or ""),
            start_date=_f(d, "start_date"),
            public_ipaddr=str(d.get("public_ipaddr") or ""),
            geolocation=str(d.get("geolocation") or ""),
            raw=d,
        )

    @property
    def age_hours(self) -> float:
        if not self.start_date:
            return 0.0
        return max(0.0, (time.time() - self.start_date) / 3600.0)

    @property
    def accrued_cost(self) -> float:
        """Rough rent cost so far (excludes bandwidth/storage extras)."""
        return self.age_hours * self.dph_total

    @property
    def has_ssh(self) -> bool:
        return bool(self.ssh_host and self.ssh_port)


def normalize_instance_payload(data: Any, instance_id: Optional[int] = None) -> dict:
    """Extract a single instance dict from the API's shape-shifting responses.

    ``GET /instances/{id}/`` may return the instance directly, wrap it as
    ``{"instances": {...}}``, or wrap a list as ``{"instances": [...]}``.
    """
    if not isinstance(data, dict):
        raise ValueError(f"unexpected instance payload: {type(data).__name__}")
    info: Any = data.get("instances", data)
    if isinstance(info, list):
        if instance_id is not None:
            for item in info:
                if isinstance(item, dict) and item.get("id") == instance_id:
                    return item
            raise LookupError(f"instance {instance_id} not found in response")
        if len(info) == 1 and isinstance(info[0], dict):
            return info[0]
        raise ValueError("ambiguous instance list payload")
    if not isinstance(info, dict) or not info:
        raise LookupError(f"instance {instance_id} not found (empty response)")
    return info
