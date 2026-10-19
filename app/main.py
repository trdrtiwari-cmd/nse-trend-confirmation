
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
import os

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent.parent

app = FastAPI(
    title="NSE Trend Confirmation",
    version="0.3.0",
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

MAX_CANDLE_AGE_SECONDS = 15 * 60


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


class DataProvider:
    def __init__(self):
        # Keep the token in Render Environment Variables only.
        self.token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

    async def snapshot(self, symbol: str) -> dict[str, Any]:
        base = {
            "symbol": symbol,
            "as_of": None,
            "checked_at": utc_now().isoformat(),
            "source": "Upstox V3 Intraday Candles (5-minute)",
            "data": empty_data(),
            "fresh": False,
            "market_data_status": "UNAVAILABLE",
            "message": "",
        }

        if not self.token:
            base["source"] = "Upstox (not configured)"
            base["message"] = (
                "UPSTOX_ACCESS_TOKEN Render Environment Variables "
                "mein configure nahi hai."
            )
            return base

        instrument = INSTRUMENTS[symbol]
        encoded_key = quote(instrument, safe="")

        url = (
            "https://api.upstox.com/v3/historical-candle/"
            f"intraday/{encoded_key}/minutes/5"
        )

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                payload = response.json()

            candles = payload.get("data", {}).get("candles", [])

            if not isinstance(candles, list) or len(candles) < 2:
                raise ValueError("Fewer than two candles returned")

            # Upstox candle format:
            # [timestamp, open, high, low, close, volume, open_interest]
            # Sort explicitly because API ordering should not be assumed.
            parsed = []

            for candle in candles:
                if not isinstance(candle, (list, tuple)) or len(candle) < 5:
                    continue

                candle_dt = parse_timestamp(candle[0])
                close = float(candle[4])

                if close <= 0:
                    continue

                parsed.append((candle_dt, candle, close))

            if len(parsed) < 2:
                raise ValueError("Not enough valid candles")

            parsed.sort(key=lambda item: item[0])

            latest_time, latest, close_now = parsed[-1]
            previous_time, previous, close_previous = parsed[-2]

            now = utc_now()
            age_seconds = (now - latest_time).total_seconds()

            # Reject future-dated and stale candles.
            fresh = 0 <= age_seconds <= MAX_CANDLE_AGE_SECONDS

            bias = (
                1 if close_now > close_previous
                else -1 if close_now < close_previous
                else 0
            )

            # Show price and timestamp even if the candle is stale.
            # Direction is trusted only if the candle is fresh.
            direction = {
                "bias": bias if fresh else None,
                "price": close_now,
                "previous_close": close_previous,
                "candle_time": latest_time.isoformat(),
                "previous_candle_time": previous_time.isoformat(),
                "age_seconds": int(age_seconds),
                "fresh": fresh,
            }

            data = empty_data()
            data["index_direction"] = direction

            base.update({
                "as_of": latest_time.isoformat(),
                "data": data,
                "fresh": fresh,
                "market_data_status": (
                    "RECEIVED" if fresh else "STALE"
                ),
            })

            if fresh:
                base["message"] = (
                    "Fresh index candle received from Upstox. "
                    "Other four required checks are not connected; "
                    "signal remains WAIT."
                )
            else:
                base["message"] = (
                    "Latest returned candle is stale or future-dated. "
                    "Its price is shown for diagnosis only. "
                    "Index direction is not trusted; signal remains WAIT."
                )

            return base

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code

            base["market_data_status"] = "API_ERROR"
            base["source"] = "Upstox API (HTTP error)"
            base["message"] = (
                f"Upstox returned HTTP {status}. Check token validity, "
                "instrument key, API permissions, and Render logs."
            )

        except httpx.HTTPError:
            base["market_data_status"] = "CONNECTION_ERROR"
            base["source"] = "Upstox API (connection error)"
            base["message"] = (
                "Upstox request failed or timed out. Check Render logs "
                "and outbound connectivity."
            )

        except (ValueError, TypeError, KeyError, IndexError):
            base["market_data_status"] = "INVALID_RESPONSE"
            base["source"] = "Upstox API (invalid response)"
            base["message"] = (
                "Upstox response is missing required fields or has an "
                "unexpected format. No trading signal generated."
            )

        return base


provider = DataProvider()


@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": app.title,
        "time": utc_now().isoformat(),
        "upstox_token_configured": bool(provider.token),
    }


@app.get("/api/snapshot/{symbol}")
async def snapshot(symbol: str):
    symbol = symbol.upper()

    if symbol not in INSTRUMENTS:
        return {
            "symbol": symbol,
            "ready": False,
            "signal": "WAIT",
            "as_of": None,
            "checked_at": utc_now().isoformat(),
            "source": "None",
            "market_data_status": "UNAVAILABLE",
            "fresh": False,
            "data": empty_data(),
            "missing": REQUIRED,
            "message": "Unsupported symbol. Choose NIFTY or BANKNIFTY.",
            "reasons": ["Unsupported symbol."],
        }

    snap = await provider.snapshot(symbol)
    data = snap["data"]

    missing = [
        key for key in REQUIRED
        if not isinstance(data.get(key), dict)
    ]

    direction = data.get("index_direction")
    index_valid = (
        isinstance(direction, dict)
        and direction.get("fresh") is True
        and direction.get("bias") in (-1, 0, 1)
    )

    ready = (
        not missing
        and snap.get("fresh") is True
        and index_valid
        and bool(snap.get("as_of"))
    )

    # Safe integration stage:
    # Do not generate BUY signals until all five checks are implemented,
    # timestamp-validated, and evaluated by a tested decision engine.
    signal = "WAIT"

    reasons = []

    if missing:
        reasons.append(
            "Missing required checks: " + ", ".join(missing) + "."
        )

    if not snap.get("fresh"):
        reasons.append("Index candle is missing, stale, or future-dated.")

    if not index_valid:
        reasons.append("Index direction is not validated.")

    reasons.append(
        "BUY signals remain disabled until all five checks are "
        "implemented, validated, and fresh."
    )

    return {
        "symbol": symbol,
        "ready": ready,
        "signal": signal,
        "as_of": snap.get("as_of"),
        "checked_at": snap.get("checked_at"),
        "source": snap.get("source"),
        "market_data_status": snap.get("market_data_status"),
        "fresh": snap.get("fresh", False),
        "data": data,
        "missing": missing,
        "message": snap.get("message"),
        "reasons": reasons,
    }
