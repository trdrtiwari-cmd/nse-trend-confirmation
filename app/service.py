"""Builds the /api/snapshot and /api/options responses (framework-free)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Awaitable

from . import logic
from .universe import CONSTITUENTS, SECTOR_INDICES, SYMBOLS
from .upstox import UpstoxClient, UpstoxError

log = logging.getLogger("nse-trend")

SOURCE = "Upstox V3 Intraday Candles (5-minute)"
FATAL = ("NOT_CONFIGURED", "CONNECTION_ERROR")


def _lookup(result: dict[str, Any], key: str) -> dict[str, Any] | None:
    return result.get(key) or result.get(key.replace("|", ":", 1))


async def _safe(coro: Awaitable[Any], label: str) -> tuple[Any, str | None]:
    """Run one check; a failure only disables that check, never the page."""
    try:
        return await coro, None
    except UpstoxError as exc:
        return None, f"{label}: {exc.message}"
    except Exception:  # noqa: BLE001 - one broken check must not break the rest
        log.exception("check failed: %s", label)
        return None, f"{label}: अप्रत्याशित error (server log देखें)।"


# ------------------------------------------------------------ index data
async def _index_candles(client: UpstoxClient, key: str, now: datetime) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    try:
        candles = logic.parse_candles(await client.intraday_candles(key))
    except UpstoxError as exc:
        if exc.kind in FATAL or exc.status == 401:
            raise

    if not candles:  # before the open / holiday: fall back to recent history
        try:
            start = now.date() - timedelta(days=7)
            candles = logic.parse_candles(await client.historical_candles(key, start, now.date()))
        except UpstoxError as exc:
            if exc.kind in FATAL or exc.status == 401:
                raise
    return candles


# ---------------------------------------------------------------- checks
async def _check_advance_decline(client: UpstoxClient, symbol: str) -> dict[str, Any] | None:
    names = CONSTITUENTS[symbol]
    keys = await client.equity_keys(names)
    if len(keys) < len(names) * 0.8:
        raise UpstoxError("API_ERROR", f"सिर्फ़ {len(keys)}/{len(names)} stocks के instrument keys मिले।")

    quotes = await client.ltp(list(keys.values()))
    pairs = []
    for key in keys.values():
        item = _lookup(quotes, key) or {}
        pairs.append((item.get("last_price"), item.get("cp")))

    result = logic.advance_decline(pairs, expected=len(names))
    if result is None:
        raise UpstoxError("API_ERROR", "पर्याप्त stocks का price/previous-close नहीं मिला।")
    return result


async def _check_sectors(client: UpstoxClient) -> dict[str, Any] | None:
    quotes = await client.ltp(list(SECTOR_INDICES.values()))
    items = []
    for name, key in SECTOR_INDICES.items():
        item = _lookup(quotes, key) or {}
        items.append((name, item.get("last_price"), item.get("cp")))

    result = logic.sector_heatmap(items)
    if result is None:
        raise UpstoxError("API_ERROR", "कम से कम 5 sector indices का data नहीं मिला।")
    return result


async def _option_checks(client: UpstoxClient, symbol: str, now: datetime) -> tuple[Any, Any, list[str]]:
    """Premium % change and CE/PE graph from the nearest-expiry ATM pair."""
    underlying = SYMBOLS[symbol]["key"]
    expiries = logic.available_expiries(await client.option_contracts(underlying), now)
    if not expiries:
        raise UpstoxError("API_ERROR", "कोई upcoming expiry नहीं मिली।")

    rows = await client.option_chain(underlying, expiries[0])
    spot = next((logic.num(r.get("underlying_spot_price")) for r in rows if logic.num(r.get("underlying_spot_price"))), None)
    picked = logic.pick_strikes(rows, spot, "ATM") if spot else None
    if not picked:
        raise UpstoxError("API_ERROR", "Option chain में ATM strike नहीं मिला।")

    _, ce_row, pe_row = picked
    ce, pe = logic.leg(ce_row, "call_options"), logic.leg(pe_row, "put_options")

    premium = logic.premium_change(ce, pe)
    reasons: list[str] = []
    if premium is None:
        reasons.append("Option Premium % Change: LTP/previous close उपलब्ध नहीं।")

    graph = None
    if ce.get("instrument_key") and pe.get("instrument_key"):
        (ce_raw, ce_err), (pe_raw, pe_err) = await asyncio.gather(
            _safe(client.intraday_candles(ce["instrument_key"]), "CE candles"),
            _safe(client.intraday_candles(pe["instrument_key"]), "PE candles"),
        )
        reasons += [e for e in (ce_err, pe_err) if e]
        if ce_raw is not None and pe_raw is not None:
            graph = logic.premium_graph(logic.parse_candles(ce_raw), logic.parse_candles(pe_raw))
            if graph is None:
                reasons.append("CE/PE Premium Graph: आज के पर्याप्त option candles नहीं हैं।")
    else:
        reasons.append("CE/PE Premium Graph: option instrument key नहीं मिली।")

    if premium:
        premium.update(expiry=expiries[0], strike=ce.get("strike"))
    if graph:
        graph.update(expiry=expiries[0], strike=ce.get("strike"))
    return premium, graph, reasons


# -------------------------------------------------------------- snapshot
async def build_snapshot(client: UpstoxClient, symbol: str, now: datetime | None = None) -> dict[str, Any]:
    now = (now or logic.now_ist()).astimezone(logic.IST)
    base = {
        "symbol": symbol,
        "label": SYMBOLS[symbol]["label"],
        "source": SOURCE,
        "checked_at": now.isoformat(),
        "as_of": None,
        "fresh": False,
        "ready": False,
        "signal": "WAIT",
        "market_open": logic.regular_market_hours(now),
        "data": {key: None for key in logic.REQUIRED},
    }

    try:
        candles = await _index_candles(client, SYMBOLS[symbol]["key"], now)
    except UpstoxError as exc:
        status = exc.kind if exc.kind in ("NOT_CONFIGURED", "CONNECTION_ERROR") else "API_ERROR"
        return {**base, "market_data_status": status, "message": exc.message, "reasons": [exc.message]}

    direction = logic.index_direction(candles, now)
    if direction is None:
        msg = "Index candle data नहीं मिला। कोई signal नहीं।"
        return {**base, "market_data_status": "UNAVAILABLE", "message": msg, "reasons": [msg]}

    (ad, ad_err), (sectors, sec_err), (options, opt_err) = await asyncio.gather(
        _safe(_check_advance_decline(client, symbol), "Advance-Decline"),
        _safe(_check_sectors(client), "Sector Heat Map"),
        _safe(_option_checks(client, symbol, now), "Option checks"),
    )
    premium, graph, opt_reasons = options if options else (None, None, [])

    data = {
        "advance_decline": ad,
        "sector_heatmap": sectors,
        "option_premium_change": premium,
        "call_put_graph": graph,
        "index_direction": direction,
    }
    signal, ready, reasons = logic.decide_signal(data, base["market_open"], direction["fresh"])
    notes = [r for r in (ad_err, sec_err, opt_err, *opt_reasons) if r]

    status = direction["status"]
    if status == "MARKET_CLOSED" and not base["market_open"]:
        message = "Market closed है - last session का data सिर्फ़ देखने के लिए है। Signal: WAIT।"
    elif status == "STALE":
        message = "Latest index candle stale या future-dated है। Price सिर्फ़ diagnosis के लिए है; signal WAIT।"
    else:
        message = " ".join(reasons[:3])

    return {
        **base,
        "as_of": direction["candle_time"],
        "fresh": direction["fresh"],
        "ready": ready,
        "signal": signal,
        "market_data_status": status,
        "message": message,
        "reasons": reasons + notes,
        "data": data,
    }


# --------------------------------------------------------------- options
async def build_options(
    client: UpstoxClient,
    symbol: str,
    moneyness: str = "ATM",
    expiry_date: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = (now or logic.now_ist()).astimezone(logic.IST)
    moneyness = (moneyness or "ATM").upper()
    result: dict[str, Any] = {"ok": False, "symbol": symbol, "moneyness": moneyness, "expiry": expiry_date or None}

    if moneyness not in ("ATM", "ITM", "OTM"):
        return {**result, "message": "Strike type ATM, ITM या OTM होना चाहिए।"}
    if expiry_date and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", expiry_date):
        return {**result, "message": "Expiry format YYYY-MM-DD होना चाहिए।"}

    underlying = SYMBOLS[symbol]["key"]
    try:
        expiries = logic.available_expiries(await client.option_contracts(underlying), now)
        result["available_expiries"] = expiries
        if not expiries:
            return {**result, "message": "कोई upcoming expiry उपलब्ध नहीं है।"}

        chosen = expiry_date if expiry_date in expiries else expiries[0]
        result["expiry"] = chosen

        rows = await client.option_chain(underlying, chosen)
    except UpstoxError as exc:
        return {**result, "message": exc.message}

    spot = next((logic.num(r.get("underlying_spot_price")) for r in rows if logic.num(r.get("underlying_spot_price"))), None)
    picked = logic.pick_strikes(rows, spot, moneyness) if spot else None
    if not picked:
        return {**result, "message": f"{chosen} expiry का option chain खाली है या spot price नहीं मिला।"}

    atm, ce_row, pe_row = picked
    if ce_row is None or pe_row is None:
        return {**result, "message": f"{moneyness} strike chain की range से बाहर है।"}

    return {
        **result,
        "ok": True,
        "spot": spot,
        "atm_strike": atm,
        "fetched_at": now.isoformat(),
        "ce": logic.leg(ce_row, "call_options"),
        "pe": logic.leg(pe_row, "put_options"),
    }
