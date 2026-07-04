"""Instance lifecycle: rent with fallback across offers, wait-ready, resolve.

The rent loop encodes the painful lessons of Vast: listed offers are routinely
already taken (create fails), and some machines accept the contract but never
boot. So: try offers best-first, attach your SSH key immediately, give each
machine a bounded time to become SSH-reachable, destroy and move on otherwise.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from . import remote
from .api import VastAPI, VastAPIError
from .models import Instance, Offer

DEFAULT_IMAGE = "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel"
# Vast's own images wrap SSH sessions in tmux; opt out so scripted SSH behaves.
DEFAULT_ONSTART = "touch ~/.no_auto_tmux"


def _log_stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def find_ssh_key(explicit: str = "") -> str:
    """Locate the private key to use: flag > $VASTKIT_SSH_KEY > conventional
    names > the newest ``~/.ssh/*.pub`` pair. Returns "" if nothing found
    (ssh will then rely on its own defaults/agent)."""
    if explicit:
        return str(Path(explicit).expanduser())
    env = os.environ.get("VASTKIT_SSH_KEY", "")
    if env:
        return str(Path(env).expanduser())
    ssh_dir = Path.home() / ".ssh"
    for name in ("id_ed25519", "id_rsa"):
        priv = ssh_dir / name
        if priv.exists() and (ssh_dir / (name + ".pub")).exists():
            return str(priv)
    pairs = [
        p.with_suffix("") for p in sorted(ssh_dir.glob("*.pub"))
        if p.with_suffix("").exists()
    ]
    if pairs:
        newest = max(pairs, key=lambda p: p.stat().st_mtime)
        return str(newest)
    return ""


def read_pubkey(private_key_path: str) -> str:
    if not private_key_path:
        return ""
    pub = Path(private_key_path + ".pub")
    return pub.read_text().strip() if pub.exists() else ""


@dataclass
class RentSpec:
    """Everything needed to turn an offer into a reachable instance."""

    image: str = DEFAULT_IMAGE
    disk: float = 60.0
    label: str = "vastkit"
    onstart: str = ""
    env: dict = field(default_factory=dict)
    runtype: str = "ssh_direc"
    ssh_key: str = ""  # private key path; pubkey is attached automatically
    attempts: int = 5
    boot_timeout: float = 600.0  # per-offer: created -> SSH reachable
    poll_interval: float = 10.0

    def full_onstart(self) -> str:
        if self.onstart:
            return DEFAULT_ONSTART + "\n" + self.onstart
        return DEFAULT_ONSTART


def rent(
    api: VastAPI,
    offers: List[Offer],
    spec: RentSpec,
    log: Optional[Callable[[str], None]] = None,
) -> Instance:
    """Rent the first workable offer from a ranked list. Returns the ready instance."""
    _log = log or _log_stderr
    if not offers:
        raise VastAPIError("no offers to rent")

    ssh_key = find_ssh_key(spec.ssh_key)
    pubkey = read_pubkey(ssh_key)
    if ssh_key:
        _log(f"Using SSH key: {ssh_key}")
    else:
        _log("Warning: no SSH keypair found — relying on keys registered in your Vast account")

    tried = 0
    for offer in offers:
        if tried >= spec.attempts:
            break
        tried += 1
        _log(
            f"[{tried}/{spec.attempts}] renting offer {offer.id}: {offer.gpu_name} "
            f"{offer.gpu_ram_gb:.0f}GB @ ${offer.dph_total:.3f}/hr ({offer.geolocation})"
        )
        try:
            instance_id = api.create_instance(
                offer_id=offer.id,
                image=spec.image,
                disk=spec.disk,
                label=spec.label,
                onstart=spec.full_onstart(),
                env=spec.env or None,
                runtype=spec.runtype,
            )
        except VastAPIError as e:
            _log(f"  offer unavailable: {e}")
            continue
        _log(f"  instance {instance_id} created; waiting for boot ...")

        if pubkey:
            try:
                api.attach_ssh_key(instance_id, pubkey)
            except VastAPIError as e:
                _log(f"  note: could not attach SSH key ({e}); "
                     "account-registered keys will be used")

        instance = _wait_ready(api, instance_id, ssh_key, spec, _log)
        if instance is not None:
            _log(
                f"  ready: ssh -p {instance.ssh_port} root@{instance.ssh_host} "
                f"(${instance.dph_total:.3f}/hr)"
            )
            return instance

        _log(f"  instance {instance_id} failed to become reachable — destroying, trying next offer")
        try:
            api.destroy_instance(instance_id)
        except VastAPIError as e:
            _log(f"  WARNING: destroy failed for {instance_id}: {e} — check the dashboard!")

    raise VastAPIError(f"all {tried} rent attempts failed")


def _wait_ready(
    api: VastAPI,
    instance_id: int,
    ssh_key: str,
    spec: RentSpec,
    log: Callable[[str], None],
) -> Optional[Instance]:
    """Poll until running + SSH answers, within spec.boot_timeout. None on timeout."""
    start = time.time()
    last_status = ""
    while time.time() - start < spec.boot_timeout:
        try:
            instance = api.get_instance(instance_id)
        except VastAPIError as e:
            log(f"  [{int(time.time() - start)}s] status query failed: {e}")
            time.sleep(spec.poll_interval)
            continue
        status = instance.actual_status or "starting"
        if status != last_status:
            log(f"  [{int(time.time() - start)}s] status={status}")
            last_status = status
        if instance.has_ssh and status == "running":
            if remote.check_ssh(instance.ssh_host, instance.ssh_port, ssh_key):
                return instance
        time.sleep(spec.poll_interval)
    return None


def resolve_instance(api: VastAPI, ident: Optional[str] = None) -> Instance:
    """Turn an optional CLI identifier into an Instance.

    With no identifier: if the account has exactly one instance, use it."""
    if ident:
        return api.get_instance(int(ident))
    instances = api.list_instances()
    if not instances:
        raise VastAPIError("no instances found — rent one with `vastkit rent`")
    if len(instances) > 1:
        ids = ", ".join(f"{i.id} ({i.gpu_name}, {i.label})" for i in instances)
        raise VastAPIError(f"multiple instances — specify an id: {ids}")
    return instances[0]


def destroy_instance(api: VastAPI, instance_id: int) -> Tuple[Optional[Instance], float]:
    """Destroy an instance; returns (last known state, accrued rent $)."""
    instance: Optional[Instance] = None
    accrued = 0.0
    try:
        instance = api.get_instance(instance_id)
        accrued = instance.accrued_cost
    except VastAPIError:
        pass
    api.destroy_instance(instance_id)
    return instance, accrued
