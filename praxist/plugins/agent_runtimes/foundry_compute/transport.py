"""Private stdlib transport for the Foundry compute coordinator contract."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit


class FoundryTransportError(RuntimeError):
    """Normalized remote coordinator failure without response internals."""


class FoundryTransport:
    """Minimal authenticated HTTP transport for the Foundry coordinator."""

    def __init__(self, server_url: str, admin_token: str, *, timeout_seconds: int = 60) -> None:
        self.server_url = server_url.rstrip("/")
        parsed = urlsplit(self.server_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
            raise ValueError("Foundry compute URL must be an HTTP(S) origin")
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("Foundry compute requires HTTPS outside loopback")
        if len(admin_token) < 16:
            raise ValueError("Foundry compute admin token is missing or too short")
        self.admin_token = admin_token
        self.timeout_seconds = timeout_seconds
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def upload(self, data: bytes, *, media_type: str, filename: str) -> dict[str, Any]:
        digest = hashlib.sha256(data).hexdigest()
        response = self._request(
            "PUT",
            f"/v1/artifacts/{digest}",
            data,
            {"Content-Type": media_type, "X-Foundry-Artifact-Filename": filename},
        )
        return _json_object(response)

    def download(self, digest: str) -> bytes:
        data = self._request("GET", f"/v1/artifacts/{digest}", b"", {})
        if hashlib.sha256(data).hexdigest() != digest:
            raise FoundryTransportError("Foundry compute artifact failed digest verification")
        return data

    def submit(self, draft: dict[str, Any]) -> dict[str, Any]:
        return self._json("POST", "/v1/admin/jobs", draft)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/v1/admin/jobs/{job_id}", None)

    def cancel(self, job_id: str, reason: str) -> dict[str, Any]:
        return self._json("POST", f"/v1/admin/jobs/{job_id}/cancel", {"reason": reason})

    def wait_job(
        self,
        job_id: str,
        *,
        timeout_seconds: int,
        stop_requested: Any = None,
        poll_seconds: float = 0.5,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while True:
            job = self.get_job(job_id)
            if job["status"] in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return job
            if callable(stop_requested) and stop_requested():
                return self.cancel(job_id, "Praxist run requested stop")
            if time.monotonic() >= deadline:
                self.cancel(job_id, "Praxist runtime wait expired")
                raise FoundryTransportError(f"Timed out waiting for Foundry compute job {job_id}")
            time.sleep(poll_seconds)

    def _json(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        body = (
            (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            if payload is not None
            else b""
        )
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        return _json_object(self._request(method, path, body, headers))

    def _request(self, method: str, path: str, body: bytes, headers: dict[str, str]) -> bytes:
        request_headers = {**headers, "Authorization": f"Bearer {self.admin_token}"}
        request = urllib.request.Request(
            self.server_url + path,
            data=body if method in {"POST", "PUT"} else None,
            headers=request_headers,
            method=method,
        )
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
                message = str(payload.get("message") or exc.reason)
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = str(exc.reason)
            raise FoundryTransportError(
                f"Foundry compute rejected request ({exc.code}): {message}"
            ) from None
        except urllib.error.URLError as exc:
            raise FoundryTransportError("Foundry compute coordinator is unavailable") from exc


def _json_object(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FoundryTransportError("Foundry compute returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise FoundryTransportError("Foundry compute returned a non-object response")
    return value
