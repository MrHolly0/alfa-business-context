from __future__ import annotations

import os
import json
import math
from urllib.parse import quote, urlsplit

import httpx


class CoreClient:
    """HTTP client for the operator-configured Core; jobs cannot choose URLs."""
    def __init__(self, base_url: str | None = None, service_token: str | None = None,
                 *, timeout: float = 10.0, client: httpx.Client | None = None):
        self.base_url = (base_url if base_url is not None else os.getenv("CORE_BASE_URL", "")).rstrip("/")
        self.token = service_token if service_token is not None else os.getenv("CORE_SERVICE_TOKEN", "")
        parsed = urlsplit(self.base_url)
        if (not self.base_url or parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("CORE_BASE_URL must be an operator-configured HTTP(S) origin")
        if not self.token: raise ValueError("CORE_SERVICE_TOKEN is required")
        timeout = float(timeout)
        if not math.isfinite(timeout): raise ValueError("timeout must be finite")
        self.timeout = min(max(timeout, .1), 30)
        self._client = client or httpx.Client(timeout=httpx.Timeout(self.timeout), follow_redirects=False)
        self._owns_client = client is None

    def _request(self, method: str, route: str, **kwargs):
        with self._client.stream(method, self.base_url + route,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            timeout=self.timeout, follow_redirects=False, **kwargs) as response:
            if response.is_redirect: raise RuntimeError("Core redirects are not accepted")
            response.raise_for_status()
            if response.status_code == 204: return None
            size = 0
            chunks = []
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 4_000_000: raise RuntimeError("Core response exceeds limit")
                chunks.append(chunk)
            return json.loads(b"".join(chunks))

    def claim(self):
        return self._request("POST", "/internal/v1/analysis-jobs/claim", json={})

    def heartbeat(self, job_id: str, generation: int):
        return self._request("POST", f"/internal/v1/analysis-jobs/{quote(job_id, safe='')}/heartbeat", json={"lease_generation": generation})

    def submit_result(self, payload: dict):
        job_id = payload.get("job_id")
        if not isinstance(job_id, str) or not job_id: raise ValueError("result requires job_id")
        return self._request("POST", f"/internal/v1/analysis-jobs/{quote(job_id, safe='')}/result", json=payload)

    def submit_failure(self, job_id: str, generation: int, code: str):
        return self._request("POST", f"/internal/v1/analysis-jobs/{quote(job_id, safe='')}/failure", json={"lease_generation": generation, "code": code})

    def close(self):
        if self._owns_client: self._client.close()
