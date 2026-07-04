"""Minimal Vast.ai REST client built on urllib — no third-party dependencies.

Endpoints and quirks mirror the behaviour proven in production tooling:
requests are throttled (Vast rate-limits aggressively), transient errors are
retried with backoff, and instance payloads are normalized.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, List, Optional

from .models import Instance, Offer, normalize_instance_payload

BASE_URL = "https://console.vast.ai/api/v0"
MIN_REQUEST_INTERVAL = 2.0  # seconds; Vast returns 429s below this
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
KEY_FILES = ("~/.vast_api_key", "~/.config/vastkit/api_key")


class VastAPIError(RuntimeError):
    """API request failed. Carries the HTTP status when available."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def resolve_api_key(explicit: Optional[str] = None) -> str:
    """Resolve the API key: explicit arg > $VAST_API_KEY > key files."""
    if explicit:
        return explicit.strip()
    env = os.environ.get("VAST_API_KEY", "").strip()
    if env:
        return env
    for candidate in KEY_FILES:
        path = Path(candidate).expanduser()
        if path.exists():
            key = path.read_text().strip()
            if key:
                return key
    raise VastAPIError(
        "Vast.ai API key not found. Get one at https://cloud.vast.ai/cli/ and either\n"
        "  export VAST_API_KEY=...   or   echo '...' > ~/.vast_api_key && chmod 600 ~/.vast_api_key"
    )


class VastAPI:
    """Thread-unsafe, throttled client for the Vast.ai v0 REST API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = BASE_URL,
        timeout: float = 60.0,
        min_interval: float = MIN_REQUEST_INTERVAL,
        max_retries: int = 4,
    ) -> None:
        self.api_key = resolve_api_key(api_key)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.min_interval = min_interval
        self.max_retries = max_retries
        self._last_request = 0.0

    # ------------------------------------------------------------------ core

    def _throttle(self) -> None:
        elapsed = time.time() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last_request = time.time()

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": "vastkit",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    text = resp.read().decode("utf-8", "replace")
                    return json.loads(text) if text.strip() else {}
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")[:500]
                if e.code in RETRYABLE_STATUSES and attempt < self.max_retries:
                    last_err = e
                    time.sleep(min(30.0, 2.0 * (attempt + 1) + random.uniform(0, 1)))
                    continue
                hint = ""
                if e.code in (401, 403):
                    hint = " (check your API key: https://cloud.vast.ai/cli/)"
                raise VastAPIError(
                    f"{method} {path} -> HTTP {e.code}{hint}: {text}", status=e.code
                ) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < self.max_retries:
                    last_err = e
                    time.sleep(min(30.0, 2.0 * (attempt + 1) + random.uniform(0, 1)))
                    continue
                raise VastAPIError(f"{method} {path} failed: {e}") from None
        raise VastAPIError(f"{method} {path} failed after retries: {last_err}")

    # -------------------------------------------------------------- accounts

    def current_user(self) -> dict:
        """Account info; includes ``credit`` balance. Cheap API-key check."""
        return self._request("GET", "/users/current/")

    # ---------------------------------------------------------------- offers

    def search_offers(self, query: dict) -> List[Offer]:
        """Search rentable offers. ``query`` is the raw filter body
        (see :func:`vastkit.query.build_query`)."""
        data = self._request("POST", "/bundles/", payload=query)
        return [Offer.from_raw(o) for o in data.get("offers", [])]

    # ------------------------------------------------------------- instances

    def create_instance(
        self,
        offer_id: int,
        image: str,
        disk: float,
        label: str = "",
        onstart: str = "",
        env: Optional[dict] = None,
        runtype: str = "ssh_direc",
    ) -> int:
        """Rent an offer. Returns the new instance (contract) id."""
        body: dict = {
            "image": image,
            "disk": disk,
            "runtype": runtype,
            "label": label,
        }
        if onstart:
            body["onstart"] = onstart
        if env:
            body["env"] = dict(env)
        data = self._request("PUT", f"/asks/{offer_id}/", payload=body)
        instance_id = data.get("new_contract")
        if not instance_id:
            raise VastAPIError(f"create_instance: no contract in response: {data}")
        return int(instance_id)

    def get_instance(self, instance_id: int) -> Instance:
        data = self._request("GET", f"/instances/{instance_id}/")
        try:
            info = normalize_instance_payload(data, instance_id)
        except (LookupError, ValueError) as e:
            raise VastAPIError(f"instance {instance_id}: {e}") from None
        return Instance.from_raw(info)

    def list_instances(self) -> List[Instance]:
        data = self._request("GET", "/instances/")
        items = data.get("instances", [])
        return [Instance.from_raw(i) for i in items if isinstance(i, dict)]

    def destroy_instance(self, instance_id: int) -> None:
        self._request("DELETE", f"/instances/{instance_id}/")

    def start_instance(self, instance_id: int) -> None:
        self._request("PUT", f"/instances/{instance_id}/", payload={"state": "running"})

    def stop_instance(self, instance_id: int) -> None:
        self._request("PUT", f"/instances/{instance_id}/", payload={"state": "stopped"})

    def attach_ssh_key(self, instance_id: int, pubkey: str) -> None:
        """Attach an SSH public key to a specific instance."""
        self._request(
            "POST", f"/instances/{instance_id}/ssh/", payload={"ssh_key": pubkey.strip()}
        )

    def set_label(self, instance_id: int, label: str) -> None:
        self._request("PUT", f"/instances/{instance_id}/", payload={"label": label})
