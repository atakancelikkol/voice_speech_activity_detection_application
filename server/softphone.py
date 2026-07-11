"""Thin proxy to the softphone client's local HTTP API.

Lets the main UI (:8080) drive calls with one button instead of sending
the user to the client UI (:8081). The client stays a separate process —
only its start/stop/status endpoints are forwarded here.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request


class SoftphoneProxy:
    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def status(self) -> dict | None:
        """Client /status, or None if the client process is not running."""
        try:
            return await asyncio.to_thread(self._request, "GET", "/status")
        except (OSError, urllib.error.URLError):
            return None

    async def start(self, mode: str, wav_path: str | None = None) -> tuple[int, dict]:
        return await self._forward("/call/start", {"mode": mode, "wav_path": wav_path})

    async def stop(self) -> tuple[int, dict]:
        return await self._forward("/call/stop", {})

    async def _forward(self, path: str, payload: dict) -> tuple[int, dict]:
        try:
            body = await asyncio.to_thread(self._request, "POST", path, payload)
            return 200, body
        except urllib.error.HTTPError as exc:  # pass the client's error through
            try:
                detail = json.loads(exc.read().decode())
            except (ValueError, OSError):
                detail = {"detail": f"softphone client error (HTTP {exc.code})"}
            return exc.code, detail
        except (OSError, urllib.error.URLError):
            return 503, {"detail": "softphone client is not running — start it with `make run-client`"}

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.base_url + path,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode())
