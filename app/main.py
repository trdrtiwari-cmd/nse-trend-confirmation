
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo
import os

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


BASE = Path(__file__).resolve().parent.parent
INDIA_TZ = ZoneInfo("Asia/Kolkata")

MAX_CANDLE_AGE_SECONDS = 15 * 60

app = FastAPI(
    title="NSE Trend Confirmation",
    version="0.5.0",
)

app.mount(
    "/static",
    StaticFiles(directory=BASE / "static"),
    name="static",
)

REQUIRED = [
    "advance_decline",
    "sector_heatmap",
    "option_premium_change",
    "call_put_graph",
    "index_direction",
]

INSTRUMENTS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}


def empty_data() -> dict[str, Any]:
    return {key: None for key in REQUIRED}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: Any) -> datetime:
    timestamp = datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )

    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return timestamp.astimezone(timezone.utc)


def market_session_finished(now: datetime | None = None) -> bool:
    """
    Conservative expiry default:
    after 15:30 IST, do not default to today's expiry.
    This is not a holiday-calendar or exchange-session validator.
    """
    current = now or datetime.now(INDIA_TZ)
    return (
        current.hour > 15
        or (current.hour == 15 and current.minute >= 30)
    )


def regular_market_hours(now: datetime | None = None) -> bool:
    """
    Approximate NSE regular session: weekdays, 09:15–15:30 IST.
    This does NOT check exchange holidays or special sessions.
    """
    current = (now or datetime.now(INDIA_TZ)).astimezone(INDIA_TZ)
    minute_of_day = current.hour * 60 + current.minute
    return current.weekday() < 5 and 9 * 60 + 15 <= minute_of_day < 15 * 60 + 30


def available_expiries(
    contracts: list[Any],
    now: datetime | None = None,
) -> list[str]:
    current = now or datetime.now(INDIA_TZ)
    today = current.date().isoformat()
    after_market = market_session_finished(current)

    values = set()
