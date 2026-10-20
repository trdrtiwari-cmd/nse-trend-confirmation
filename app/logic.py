"""Pure calculation logic: no network, no web framework.

Everything here is deterministic and unit-tested (see tests/test_logic.py).
Missing or unverifiable data always produces ``None`` / WAIT - never a guess.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

# India has no daylight saving, so a fixed +05:30 offset is exact. Using it
# (instead of zoneinfo) also avoids the missing-tzdata crash on Windows.
IST = timezone(timedelta(hours=5, minutes=30), "IST")

MARKET_OPEN_MIN = 9 * 60 + 15      # 09:15 IST
MARKET_CLOSE_MIN = 15 * 60 + 30    # 15:30 IST
MAX_CANDLE_AGE_SECONDS = 15 * 60
FUTURE_TOLERANCE_SECONDS = 120

REQUIRED = [
    "advance_decline",
    "sector_heatmap",
    "option_premium_change",
    "call_put_graph",
    "index_direction",
]

# Tunable thresholds
AD_BULL_RATIO = 1.5
AD_BEAR_RATIO = 1 / 1.5
SECTOR_SHARE = 0.65
PREMIUM_DIFF_PCT = 5.0
GRAPH_DIFF_PCT = 3.0
INDEX_MIN_MOVE_PCT = 0.05


# --------------------------------------------------------------- helpers
def num(value: Any) -> float | None:
    """Convert to a finite float, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def now_ist() -> datetime:
    return datetime.now(IST)


def parse_timestamp(value: Any) -> datetime:
    """ISO string or epoch seconds/milliseconds -> aware datetime (IST)."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value > 1e11 else value
        return datetime.fromtimestamp(seconds, timezone.utc).astimezone(IST)

    text = str(value).strip().replace("Z", "+00:00")
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=IST)
    return stamp.astimezone(IST)


def minute_of_day(moment: datetime) -> int:
    return moment.hour * 60 + moment.minute


def market_session_finished(now: datetime | None = None) -> bool:
    """True from 15:30 IST. Not a holiday-calendar check."""
    current = (now or now_ist()).astimezone(IST)
    return minute_of_day(current) >= MARKET_CLOSE_MIN


def regular_market_hours(now: datetime | None = None) -> bool:
    """Approximate NSE session: weekdays 09:15-15:30 IST (no holiday check)."""
    current = (now or now_ist()).astimezone(IST)
    return (
        current.weekday() < 5
        and MARKET_OPEN_MIN <= minute_of_day(current) < MARKET_CLOSE_MIN
    )


def last_trading_day(now: datetime | None = None) -> date:
    """Most recent weekday whose session has started (holidays ignored)."""
    current = (now or now_ist()).astimezone(IST)
    day = current.date()
    if current.weekday() < 5 and minute_of_day(current) < MARKET_OPEN_MIN:
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def available_expiries(contracts: Iterable[Any], now: datetime | None = None) -> list[str]:
    """Sorted future expiry dates (YYYY-MM-DD) from option contracts.

    After 15:30 IST today's expiry is no longer offered.
    """
    current = (now or now_ist()).astimezone(IST)
    today = current.date()
    after_market = market_session_finished(current)

    values: set[str] = set()
    for item in contracts or []:
        raw = item.get("expiry") if isinstance(item, dict) else item
        if raw in (None, ""):
            continue
        try:
            day = parse_timestamp(raw).date()
        except (ValueError, TypeError, OverflowError, OSError):
            continue
        if day > today or (day == today and not after_market):
            values.add(day.isoformat())
    return sorted(values)


# --------------------------------------------------------------- candles
def parse_candles(raw: Iterable[Any]) -> list[dict[str, Any]]:
    """Upstox candle rows [time, o, h, l, c, volume, oi] -> ascending dicts."""
    out: list[dict[str, Any]] = []
    for row in raw or []:
        try:
            stamp = parse_timestamp(row[0])
        except (IndexError, KeyError, TypeError, ValueError, OverflowError, OSError):
            continue
        try:
            o, h, l, c = (num(x) for x in row[1:5])
        except TypeError:
            continue
        if None in (o, h, l, c):
            continue
        out.append({
            "time": stamp, "open": o, "high": h, "low": l, "close": c,
            "volume": num(row[5]) if len(row) > 5 else None,
        })
    out.sort(key=lambda r: r["time"])
    return out


def latest_session(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Candles belonging to the most recent trading date present."""
    if not candles:
        return []
    day = candles[-1]["time"].date()
    return [c for c in candles if c["time"].date() == day]


def ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    k = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result


def candle_status(latest: datetime, now: datetime | None = None) -> tuple[str, bool, float]:
    """(status, fresh, age_seconds) for the newest candle start time."""
    current = (now or now_ist()).astimezone(IST)
    age = (current - latest).total_seconds()

    if age < -FUTURE_TOLERANCE_SECONDS:
        return "STALE", False, age          # future-dated: never trust
    if regular_market_hours(current):
        if age <= MAX_CANDLE_AGE_SECONDS:
            return "RECEIVED", True, age
        return "STALE", False, age
    if latest.astimezone(IST).date() == last_trading_day(current):
        return "MARKET_CLOSED", False, age   # genuine last-session data
    return "STALE", False, age


def index_direction(candles: list[dict[str, Any]], now: datetime | None = None) -> dict[str, Any] | None:
    session = latest_session(candles)
    if not session:
        return None

    closes = [c["close"] for c in session]
    price = closes[-1]
    session_open = session[0]["open"]
    status, fresh, age = candle_status(session[-1]["time"], now)

    bias: int | None = None
    change_pct: float | None = None
    if session_open:
        change_pct = (price - session_open) / session_open * 100
    if len(session) >= 3 and change_pct is not None:
        average = ema(closes, 9)
        if change_pct >= INDEX_MIN_MOVE_PCT and price > average:
            bias = 1
        elif change_pct <= -INDEX_MIN_MOVE_PCT and price < average:
            bias = -1
        else:
            bias = 0

    return {
        "price": round(price, 2),
        "session_open": round(session_open, 2),
        "change_pct": None if change_pct is None else round(change_pct, 2),
        "bias": bias,
        "candle_time": session[-1]["time"].isoformat(),
        "age_seconds": int(age),
        "candles": len(session),
        "status": status,
        "fresh": fresh,
    }


# ---------------------------------------------------------------- checks
def advance_decline(pairs: list[tuple[Any, Any]], expected: int, min_coverage: float = 0.8) -> dict[str, Any] | None:
    """pairs = [(last_price, previous_close)]."""
    advances = declines = unchanged = 0
    for last, prev in pairs:
        last_n, prev_n = num(last), num(prev)
        if last_n is None or prev_n is None or prev_n <= 0:
            continue
        if last_n > prev_n:
            advances += 1
        elif last_n < prev_n:
            declines += 1
        else:
            unchanged += 1

    total = advances + declines + unchanged
    if expected <= 0 or total < expected * min_coverage:
        return None

    ratio = float(advances) if declines == 0 else advances / declines
    if advances == 0 and declines == 0:
        bias = 0
    elif ratio >= AD_BULL_RATIO:
        bias = 1
    elif ratio <= AD_BEAR_RATIO:
        bias = -1
    else:
        bias = 0

    return {
        "ratio": round(ratio, 2), "advances": advances, "declines": declines,
        "unchanged": unchanged, "counted": total, "expected": expected, "bias": bias,
    }


def sector_heatmap(items: list[tuple[str, Any, Any]], min_sectors: int = 5) -> dict[str, Any] | None:
    """items = [(name, last, previous_close)]."""
    sectors = []
    for name, last, prev in items:
        last_n, prev_n = num(last), num(prev)
        if last_n is None or prev_n is None or prev_n <= 0:
            continue
        sectors.append({"name": name, "pct": round((last_n - prev_n) / prev_n * 100, 2)})

    if len(sectors) < min_sectors:
        return None

    up = sum(1 for s in sectors if s["pct"] > 0)
    down = sum(1 for s in sectors if s["pct"] < 0)
    average = sum(s["pct"] for s in sectors) / len(sectors)
    n = len(sectors)

    if up / n >= SECTOR_SHARE and average > 0:
        bias = 1
    elif down / n >= SECTOR_SHARE and average < 0:
        bias = -1
    else:
        bias = 0

    sectors.sort(key=lambda s: s["pct"], reverse=True)
    return {"bias": bias, "up": up, "down": down, "average_pct": round(average, 2), "sectors": sectors}


def _pct(now_value: Any, base: Any) -> float | None:
    a, b = num(now_value), num(base)
    if a is None or b is None or b <= 0:
        return None
    return (a - b) / b * 100


def premium_change(ce: dict[str, Any], pe: dict[str, Any]) -> dict[str, Any] | None:
    """CE/PE premium % change versus previous close."""
    ce_pct = _pct(ce.get("ltp"), ce.get("close_price"))
    pe_pct = _pct(pe.get("ltp"), pe.get("close_price"))
    if ce_pct is None or pe_pct is None:
        return None

    diff = ce_pct - pe_pct
    bias = 1 if diff >= PREMIUM_DIFF_PCT else -1 if diff <= -PREMIUM_DIFF_PCT else 0
    return {"ce_pct": round(ce_pct, 2), "pe_pct": round(pe_pct, 2), "bias": bias}


def premium_graph(ce_candles: list[dict[str, Any]], pe_candles: list[dict[str, Any]], recent: int = 6) -> dict[str, Any] | None:
    """Session-long and recent CE vs PE premium trend from 5-min candles."""
    ce_s, pe_s = latest_session(ce_candles), latest_session(pe_candles)
    if len(ce_s) < 3 or len(pe_s) < 3:
        return None

    def trend(session: list[dict[str, Any]]) -> tuple[float | None, float | None]:
        whole = _pct(session[-1]["close"], session[0]["open"])
        base = session[-recent - 1]["close"] if len(session) > recent else session[0]["open"]
        return whole, _pct(session[-1]["close"], base)

    ce_all, ce_rec = trend(ce_s)
    pe_all, pe_rec = trend(pe_s)
    if None in (ce_all, ce_rec, pe_all, pe_rec):
        return None

    diff, rdiff = ce_all - pe_all, ce_rec - pe_rec
    if diff >= GRAPH_DIFF_PCT and rdiff > 0:
        bias = 1
    elif diff <= -GRAPH_DIFF_PCT and rdiff < 0:
        bias = -1
    else:
        bias = 0

    return {
        "ce_session_pct": round(ce_all, 2), "pe_session_pct": round(pe_all, 2),
        "ce_recent_pct": round(ce_rec, 2), "pe_recent_pct": round(pe_rec, 2),
        "bias": bias,
    }


# ---------------------------------------------------------- option chain
def pick_strikes(rows: list[dict[str, Any]], spot: float, moneyness: str) -> tuple[float, dict | None, dict | None] | None:
    """Return (atm_strike, ce_row, pe_row) for ATM / nearest ITM / nearest OTM."""
    by_strike: dict[float, dict[str, Any]] = {}
    for row in rows or []:
        strike = num(row.get("strike_price"))
        if strike is not None:
            by_strike[strike] = row
    if not by_strike or num(spot) is None:
        return None

    strikes = sorted(by_strike)
    i = min(range(len(strikes)), key=lambda k: abs(strikes[k] - spot))
    shift = {"ATM": 0, "ITM": 1, "OTM": -1}.get(moneyness.upper(), 0)
    # CE is ITM below spot, PE is ITM above spot.
    ce_i, pe_i = i - shift, i + shift
    ce_row = by_strike[strikes[ce_i]] if 0 <= ce_i < len(strikes) else None
    pe_row = by_strike[strikes[pe_i]] if 0 <= pe_i < len(strikes) else None
    return strikes[i], ce_row, pe_row


def leg(row: dict[str, Any] | None, side: str) -> dict[str, Any]:
    """Flatten one option leg ('call_options' / 'put_options')."""
    if not row:
        return {}
    block = row.get(side) or {}
    market = block.get("market_data") or {}
    greeks = block.get("option_greeks") or {}
    return {
        "strike": num(row.get("strike_price")),
        "instrument_key": block.get("instrument_key"),
        "ltp": num(market.get("ltp")),
        "close_price": num(market.get("close_price")),
        "volume": num(market.get("volume")),
        "oi": num(market.get("oi")),
        "prev_oi": num(market.get("prev_oi")),
        "iv": num(greeks.get("iv")),
        "delta": num(greeks.get("delta")),
        "theta": num(greeks.get("theta")),
        "gamma": num(greeks.get("gamma")),
        "vega": num(greeks.get("vega")),
    }


# ---------------------------------------------------------------- signal
def decide_signal(data: dict[str, Any], market_open: bool, fresh: bool) -> tuple[str, bool, list[str]]:
    """(signal, ready, reasons). BUY only if every check agrees on a live market."""
    reasons: list[str] = []

    if not market_open:
        reasons.append("Market closed है - signal सिर्फ़ regular session (09:15-15:30 IST) में बनता है।")
    elif not fresh:
        reasons.append("Latest index candle stale या future-dated है।")

    names = {
        "advance_decline": "Advance-Decline", "sector_heatmap": "Sector Heat Map",
        "option_premium_change": "Option Premium % Change",
        "call_put_graph": "CE/PE Premium Graph", "index_direction": "Index Direction",
    }
    biases = []
    for key in REQUIRED:
        value = data.get(key)
        if not value or value.get("bias") is None:
            reasons.append(f"{names[key]}: data उपलब्ध नहीं।")
        else:
            biases.append(value["bias"])

    ready = market_open and fresh and len(biases) == len(REQUIRED)
    if not ready:
        return "WAIT", False, reasons

    if all(b == 1 for b in biases):
        return "BUY CE", True, ["सभी 5 checks bullish हैं।"]
    if all(b == -1 for b in biases):
        return "BUY PE", True, ["सभी 5 checks bearish हैं।"]
    return "WAIT", True, ["सभी checks एक दिशा में नहीं हैं।"]
