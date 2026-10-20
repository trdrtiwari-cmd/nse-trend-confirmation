"""Thin Upstox API client (read-only market data; never places orders)."""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

BASE_URL = "https://api.upstox.com"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
CACHE_TTL_SECONDS = 4                     # shields the API from rapid repeat clicks
INSTRUMENT_TTL_SECONDS = 24 * 3600
INSTRUMENT_CACHE = Path(tempfile.gettempdir()) / "nse_trend_equity_keys.json"


class UpstoxError(Exception):
    """kind: NOT_CONFIGURED | API_ERROR | CONNECTION_ERROR"""

    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status


def get_token() -> str:
    return (
        os.environ.get("UPSTOX_ACCESS_TOKEN")
        or os.environ.get("UPSTOX_TOKEN")
        or ""
    ).strip()


def _error_text(payload: Any, fallback: str) -> str:
    try:
        errors = payload.get("errors") or []
        if errors and errors[0].get("message"):
            return str(errors[0]["message"])
    except AttributeError:
        pass
    return fallback


class UpstoxClient:
    def __init__(self, token: str | None = None):
        self._token = token
        self._http: Any = None
        self._cache: dict[str, tuple[float, Any]] = {}
        self._equity_keys: dict[str, str] = {}
        self._equity_loaded_at = 0.0
        self._equity_lock = asyncio.Lock()

    @property
    def token(self) -> str:
        return self._token if self._token is not None else get_token()

    # ----------------------------------------------------------- transport
    async def _fetch(self, url: str, params: dict[str, Any] | None, auth: bool = True) -> Any:
        """One HTTP GET -> parsed JSON. Override in tests."""
        import httpx  # imported lazily so pure logic/tests need no network libs

        headers = {"Accept": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token}"

        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0))

        try:
            response = await self._http.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise UpstoxError("CONNECTION_ERROR", f"Upstox से connection नहीं हो सका: {exc.__class__.__name__}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code == 401:
            raise UpstoxError(
                "API_ERROR",
                "Upstox access token invalid/expired है (token रोज़ लगभग 3:30 AM IST पर expire होता है)। "
                "नया UPSTOX_ACCESS_TOKEN डालकर redeploy करें।",
                401,
            )
        if response.status_code == 429:
            raise UpstoxError("API_ERROR", "Upstox rate limit - कुछ सेकंड बाद फिर कोशिश करें।", 429)
        if response.status_code >= 400:
            raise UpstoxError("API_ERROR", _error_text(payload, f"Upstox HTTP {response.status_code}"), response.status_code)
        if payload is None:
            raise UpstoxError("API_ERROR", "Upstox से invalid response मिला।", response.status_code)
        return payload

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.token:
            raise UpstoxError(
                "NOT_CONFIGURED",
                "UPSTOX_ACCESS_TOKEN environment variable सेट नहीं है। README देखें।",
            )

        cache_key = path + "?" + json.dumps(params or {}, sort_keys=True)
        hit = self._cache.get(cache_key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
            return hit[1]

        payload = await self._fetch(BASE_URL + path, params)
        if isinstance(payload, dict) and payload.get("status") not in (None, "success"):
            raise UpstoxError("API_ERROR", _error_text(payload, "Upstox ने error status लौटाया।"))

        if len(self._cache) > 200:
            self._cache.clear()
        self._cache[cache_key] = (time.monotonic(), payload)
        return payload

    # ----------------------------------------------------------- endpoints
    async def intraday_candles(self, key: str, minutes: int = 5) -> list[Any]:
        path = f"/v3/historical-candle/intraday/{quote(key, safe='')}/minutes/{minutes}"
        payload = await self.get_json(path)
        return ((payload or {}).get("data") or {}).get("candles") or []

    async def historical_candles(self, key: str, start: date, end: date, minutes: int = 5) -> list[Any]:
        path = (
            f"/v3/historical-candle/{quote(key, safe='')}/minutes/{minutes}/"
            f"{end.isoformat()}/{start.isoformat()}"
        )
        payload = await self.get_json(path)
        return ((payload or {}).get("data") or {}).get("candles") or []

    async def ltp(self, keys: list[str]) -> dict[str, dict[str, Any]]:
        """{instrument_key: {last_price, cp}} using the V3 LTP endpoint."""
        if not keys:
            return {}
        payload = await self.get_json("/v3/market-quote/ltp", {"instrument_key": ",".join(keys)})
        result: dict[str, dict[str, Any]] = {}
        for symbol_key, item in ((payload or {}).get("data") or {}).items():
            if not isinstance(item, dict):
                continue
            # Index under both spellings: "NSE_EQ|INE..." and "NSE_EQ:SYMBOL".
            result[symbol_key] = item
            if item.get("instrument_token"):
                result[item["instrument_token"]] = item
        return result

    async def option_contracts(self, underlying_key: str) -> list[Any]:
        payload = await self.get_json("/v2/option/contract", {"instrument_key": underlying_key})
        return (payload or {}).get("data") or []

    async def option_chain(self, underlying_key: str, expiry_date: str) -> list[dict[str, Any]]:
        payload = await self.get_json(
            "/v2/option/chain",
            {"instrument_key": underlying_key, "expiry_date": expiry_date},
        )
        return (payload or {}).get("data") or []

    # --------------------------------------------------- equity key lookup
    async def equity_keys(self, symbols: list[str]) -> dict[str, str]:
        """Map trading symbols -> instrument keys using Upstox's public instrument file."""
        wanted = set(symbols)
        async with self._equity_lock:
            fresh = time.time() - self._equity_loaded_at < INSTRUMENT_TTL_SECONDS
            if not (fresh and wanted <= set(self._equity_keys)):
                await self._load_equity_keys(wanted)
            return {s: self._equity_keys[s] for s in symbols if s in self._equity_keys}

    async def _load_equity_keys(self, wanted: set[str]) -> None:
        try:
            cached = json.loads(INSTRUMENT_CACHE.read_text())
            if (
                time.time() - cached.get("saved_at", 0) < INSTRUMENT_TTL_SECONDS
                and wanted <= set(cached.get("keys", {}))
            ):
                self._equity_keys = cached["keys"]
                self._equity_loaded_at = cached["saved_at"]
                return
        except (OSError, ValueError, AttributeError):
            pass

        raw = await self._download_instruments()
        keys = await asyncio.to_thread(_extract_equity_keys, raw, wanted)
        del raw
        if keys:
            self._equity_keys = keys
            self._equity_loaded_at = time.time()
            try:
                INSTRUMENT_CACHE.write_text(json.dumps({"saved_at": self._equity_loaded_at, "keys": keys}))
            except OSError:
                pass

    async def _download_instruments(self) -> bytes:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=8.0), follow_redirects=True) as http:
                response = await http.get(INSTRUMENTS_URL)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            raise UpstoxError("CONNECTION_ERROR", f"Upstox instrument file download नहीं हुई: {exc.__class__.__name__}") from exc


def _extract_equity_keys(raw: bytes, wanted: set[str]) -> dict[str, str]:
    try:
        rows = json.loads(gzip.decompress(raw))
    except (OSError, ValueError):
        return {}
    found: dict[str, str] = {}
    for row in rows:
        if row.get("segment") == "NSE_EQ" and row.get("instrument_type") == "EQ":
            symbol = row.get("trading_symbol")
            if symbol in wanted and row.get("instrument_key"):
                found[symbol] = row["instrument_key"]
    return found
