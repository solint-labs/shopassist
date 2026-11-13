"""Thin async client for Meta's Graph API.

All three channels go through the Graph API, so we centralise:
  * the API version (Meta retires versions roughly two years after the next one ships),
  * auth (Bearer header rather than ?access_token= so tokens don't land in proxy logs),
  * error handling, with Meta's error code surfaced for callers,
  * dry-run mode for local development.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.logging_config import log


class GraphAPIError(RuntimeError):
    def __init__(self, status: int, body: dict[str, Any] | str):
        self.status = status
        self.body = body
        err = body.get("error", {}) if isinstance(body, dict) else {}
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        super().__init__(f"Graph API {status}: {err.get('message') or body}")

    @property
    def retryable(self) -> bool:
        # 5xx, throttling (4, 17, 32, 613, 80007 = WA rate limit) are worth retrying.
        return self.status >= 500 or self.code in {1, 2, 4, 17, 32, 613, 80007, 130429}


class GraphClient:
    def __init__(
        self,
        host: str,
        version: str,
        http: httpx.AsyncClient | None = None,
        dry_run: bool = False,
    ):
        self.base = f"{host.rstrip('/')}/{version}"
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self.dry_run = dry_run

    async def post(self, path: str, token: str, json: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base}/{path.lstrip('/')}"
        if self.dry_run:
            log.info("graph.dry_run", url=url, body=json)
            return {"dry_run": True}
        resp = await self.http.post(url, json=json, headers={"Authorization": f"Bearer {token}"})
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        if resp.status_code >= 400:
            raise GraphAPIError(resp.status_code, body)
        return body if isinstance(body, dict) else {"raw": body}

    async def aclose(self) -> None:
        await self.http.aclose()
