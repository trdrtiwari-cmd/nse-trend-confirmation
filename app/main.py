
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
app = FastAPI(title="NSE Trend Confirmation", version="0.2.0")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

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


class DataProvider:
    def __init__(self):
        self.token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()

    async def snapshot(self, symbol: str) -> dict[str, Any]:
        empty_data = {key: None for key in REQUIRED}

        if not self.token:
            return {
                "symbol": symbol,
                "as_of": None,
                "source": "Upstox (not configured)",
                "data": empty_data,
                "message": (
                    "Render Environment Variables mein "
                    "UPSTOX_ACCESS_TOKEN set nahi hai."
                ),
                "fresh": False,
            }

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
                raise ValueError("API returned fewer than two candles.")

            # Upstox candle format:
            # [timestamp, open, high, low, close, volume, open_interest]
            latest = candles[0]
            previous = candles[1]

            if len(latest) < 5 or len(previous) < 5:
                raise ValueError("Unexpected candle format.")

            candle_time = datetime.fromisoformat(
                str(latest[0]).replace("Z", "+00:00")
            )
            if candle_time.tzinfo is None:
                candle_time = candle_time.replace(tzinfo=timezone.utc)

            now = datetime.now(timezone.utc)
            age_seconds = (now - candle_time.astimezone(timezone.utc)).total_seconds()

            # Reject future-dated or stale candles.
            fresh = 0 <= age_seconds <= MAX_CANDLE_AGE_SECONDS

            close_now = float(latest[4])
            close_previous = float(previous[4])

            if not all(map(lambda x: x > 0, [close_now, close_previous])):
                raise ValueError("Invalid candle close price.")

            bias = (
                1 if close_now > close_previous
                else -1 if close_now < close_previous
                else 0
            )

            # Keep stale candles visible for diagnosis, but don't use
            # stale price direction to generate a trading signal.
            direction = {
                "bias": bias if fresh else None,
                "price": close_now,
                "previous_close": close_previous,
                "candle_time": candle_time.isoformat(),
                "age_seconds": int(max(0, age_seconds)),
            }

            data = dict(empty_data)
            if fresh:
                data["index_direction"] = direction

            if fresh:
                message = (
                    "Index candles received from Upstox. "
                    "Other required checks are not connected yet; signal remains WAIT."
                )
            else:
                message = (
                    "Upstox returned an old or future-dated candle. "
                    "Index direction is not trusted; signal remains WAIT."
                )

            return {
                "symbol": symbol,
                "as_of": candle_time.isoformat(),
                "source": "Upstox V3 Intraday Candles (5-minute)",
                "data": data,
                "message": message,
                "fresh": fresh,
            }

        except httpx.HTTPStatusError as exc:
            # Do not return authorization headers or token in the response.
            status = exc.response.status_code
            message = (
                f"Upstox API returned HTTP {status}. "
                "Token permissions, instrument key, or API availability "
                "may need checking."
            )
        except httpx.HTTPError:
            message = (
                "Upstox API connection failed or timed out. "
                "Check Render logs and service connectivity."
            )
        except (ValueError, TypeError, KeyError, IndexError):
            message = (
                "Upstox response was missing or had an unexpected format. "
                "No trading signal generated."
            )

        return {
            "symbol": symbol,
            "as_of": None,
            "source": "Upstox API (error)",
            "data": empty_data,
            "message": message,
            "fresh": False,
        }


provider = DataProvider()


@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "app": app.title,
        "time": datetime.now(timezone.utc).isoformat(),
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
            "message": "Unsupported symbol. Choose NIFTY or BANKNIFTY.",
            "data": {key: None for key in REQUIRED},
            "missing": REQUIRED,
            "source": "None",
            "as_of": None,
            "reasons": ["Unsupported symbol."],
        }

    snap = await provider.snapshot(symbol)
    data = snap["data"]

    missing = [
        key for key in REQUIRED
        if not isinstance(data.get(key), dict)
    ]

    ready = (
        not missing
        and snap.get("fresh") is True
        and bool(snap.get("as_of"))
    )

    # This integration stage intentionally does not issue BUY signals.
    # The other four data sources must be implemented and validated first.
    signal = "WAIT"
    reasons = []

    if missing:
        reasons.append(
            "Missing required checks: " + ", ".join(missing) + "."
        )

    if not snap.get("fresh"):
        reasons.append("Index candle is missing or stale.")

    reasons.append(
        "Signal generation remains disabled until all five data checks "
        "are connected, validated, and fresh."
    )

    return {
        "symbol": symbol,
        "ready": ready,
        "signal": signal,
        "as_of": snap.get("as_of"),
        "source": snap.get("source"),
        "data": data,
        "missing": missing,
        "message": snap.get("message"),
        "reasons": reasons,
    }
