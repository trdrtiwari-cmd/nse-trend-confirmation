
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


def available_expiries(
    contracts: list[Any],
    now: datetime | None = None,
) -> list[str]:
    current = now or datetime.now(INDIA_TZ)
    today = current.date().isoformat()
    after_market = market_session_finished(current)

    values = set()

    for item in contracts:
        if not isinstance(item, dict):
            continue

        raw_expiry = item.get("expiry")
        if not raw_expiry:
            continue

        expiry_text = str(raw_expiry)

        # Expected API expiry format: YYYY-MM-DD.
        try:
            datetime.strptime(expiry_text, "%Y-%m-%d")
        except ValueError:
            continue

        if expiry_text > today:
            values.add(expiry_text)
        elif expiry_text == today and not after_market:
            values.add(expiry_text)

    return sorted(values)


class DataProvider:
    def __init__(self):
        # Store the token only in Render Environment Variables.
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

            if not isinstance(candles, list):
                raise ValueError("Invalid candle response")

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
                raise ValueError("Fewer than two valid candles")

            parsed.sort(key=lambda item: item[0])

            latest_time, latest, close_now = parsed[-1]
            previous_time, previous, close_previous = parsed[-2]

            age_seconds = (utc_now() - latest_time).total_seconds()
            fresh = 0 <= age_seconds <= MAX_CANDLE_AGE_SECONDS

            bias = (
                1 if close_now > close_previous
                else -1 if close_now < close_previous
                else 0
            )

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
                "market_data_status": "RECEIVED" if fresh else "STALE",
            })

            if fresh:
                base["message"] = (
                    "Index candle received. Other four required checks "
                    "are not connected; signal remains WAIT."
                )
            else:
                base["message"] = (
                    "Latest index candle is stale or future-dated. "
                    "Price is for diagnosis only; signal remains WAIT."
                )

            return base

        except httpx.HTTPStatusError as exc:
            base["market_data_status"] = "API_ERROR"
            base["source"] = "Upstox API (HTTP error)"
            base["message"] = (
                f"Upstox returned HTTP {exc.response.status_code}. "
                "Check token validity, instrument key, permissions, "
                "and Render logs."
            )

        except httpx.HTTPError:
            base["market_data_status"] = "CONNECTION_ERROR"
            base["source"] = "Upstox API (connection error)"
            base["message"] = (
                "Upstox request failed or timed out. Check Render logs."
            )

        except (ValueError, TypeError, KeyError, IndexError):
            base["market_data_status"] = "INVALID_RESPONSE"
            base["source"] = "Upstox API (invalid response)"
            base["message"] = (
                "Upstox candle response is invalid. "
                "No trading signal generated."
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
        "version": app.version,
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

    # Deliberately WAIT-only until all checks and the decision engine
    # are implemented, validated, and tested.
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


def option_details(
    row: dict[str, Any] | None,
    side: str,
) -> dict[str, Any] | None:
    if not row:
        return None

    option_key = "call_options" if side == "CE" else "put_options"
    option = row.get(option_key) or {}
    market = option.get("market_data") or {}
    greeks = option.get("option_greeks") or {}

    current_oi = market.get("oi")
    previous_oi = market.get("prev_oi")

    oi_change = None
    oi_change_pct = None

    try:
        current_number = float(current_oi)
        previous_number = float(previous_oi)

        oi_change = current_number - previous_number

        if previous_number != 0:
            oi_change_pct = (
                oi_change / abs(previous_number)
            ) * 100
    except (TypeError, ValueError):
        pass

    return {
        "type": side,
        "strike": row.get("strike_price"),
        "instrument_key": option.get("instrument_key"),
        "ltp": market.get("ltp"),
        "close_price": market.get("close_price"),
        "volume": market.get("volume"),
        "oi": current_oi,
        "prev_oi": previous_oi,
        "oi_change": oi_change,
        "oi_change_pct": oi_change_pct,
        "iv": greeks.get("iv", market.get("iv")),
        "delta": greeks.get("delta"),
        "theta": greeks.get("theta"),
        "gamma": greeks.get("gamma"),
        "vega": greeks.get("vega"),
    }


@app.get("/api/options/{symbol}")
async def options_snapshot(
    symbol: str,
    expiry_date: str | None = None,
    moneyness: str = Query(
        default="ATM",
        pattern="^(ATM|ITM|OTM)$",
    ),
):
    symbol = symbol.upper()

    if symbol not in INSTRUMENTS:
        return {
            "ok": False,
            "message": "Choose NIFTY or BANKNIFTY.",
        }

    if not provider.token:
        return {
            "ok": False,
            "message": "Upstox token is not configured in Render.",
        }

    if expiry_date:
        try:
            datetime.strptime(expiry_date, "%Y-%m-%d")
        except ValueError:
            return {
                "ok": False,
                "message": "expiry_date must use YYYY-MM-DD format.",
            }

    instrument = INSTRUMENTS[symbol]

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {provider.token}",
    }

    now_ist = datetime.now(INDIA_TZ)

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            contracts_response = await client.get(
                "https://api.upstox.com/v2/option/contract",
                headers=headers,
                params={"instrument_key": instrument},
            )
            contracts_response.raise_for_status()
            contracts_payload = contracts_response.json()

            contracts = contracts_payload.get("data", [])

            if not isinstance(contracts, list):
                raise ValueError("Invalid contracts response")

            # After 15:30 IST, exclude today's expiry from the default
            # list. The exchange holiday calendar is not checked here.
            expiries = available_expiries(contracts, now_ist)

            if expiry_date:
                if expiry_date not in expiries:
                    return {
                        "ok": False,
                        "symbol": symbol,
                        "message": (
                            "Expiry is not in the currently selectable "
                            "expiry list. Refresh and choose an available expiry."
                        ),
                        "available_expiries": expiries,
                    }

                selected_expiry = expiry_date
            else:
                if not expiries:
                    return {
                        "ok": False,
                        "symbol": symbol,
                        "message": (
                            "No selectable current or future expiry "
                            "was returned by Upstox."
                        ),
                    }

                selected_expiry = expiries[0]

            chain_response = await client.get(
                "https://api.upstox.com/v2/option/chain",
                headers=headers,
                params={
                    "instrument_key": instrument,
                    "expiry_date": selected_expiry,
                },
            )
            chain_response.raise_for_status()
            chain_payload = chain_response.json()

        chain = chain_payload.get("data", [])

        if not isinstance(chain, list) or not chain:
            return {
                "ok": False,
                "symbol": symbol,
                "expiry": selected_expiry,
                "available_expiries": expiries,
                "message": "Option chain is empty.",
            }

        valid_rows = []

        for row in chain:
            if not isinstance(row, dict):
                continue

            try:
                strike = float(row["strike_price"])
                spot = float(row["underlying_spot_price"])
            except (KeyError, TypeError, ValueError):
                continue

            if strike > 0 and spot > 0:
                valid_rows.append((strike, spot, row))

        if not valid_rows:
            return {
                "ok": False,
                "symbol": symbol,
                "message": "No valid strike or spot price found.",
            }

        # Use the most frequent underlying spot if the API has repeated rows.
        spot = valid_rows[0][1]
        strikes = sorted({item[0] for item in valid_rows})
        atm_strike = min(strikes, key=lambda strike: abs(strike - spot))

        ce_candidates = []
        pe_candidates = []

        for strike, _, row in valid_rows:
            if moneyness == "ATM":
                if strike == atm_strike:
                    ce_candidates.append((0, strike, row))
                    pe_candidates.append((0, strike, row))

            elif moneyness == "ITM":
                # Call ITM: strike below or equal to spot.
                if strike <= spot:
                    ce_candidates.append((spot - strike, strike, row))

                # Put ITM: strike above or equal to spot.
                if strike >= spot:
                    pe_candidates.append((strike - spot, strike, row))

            else:  # OTM
                # Call OTM: strike above or equal to spot.
                if strike >= spot:
                    ce_candidates.append((strike - spot, strike, row))

                # Put OTM: strike below or equal to spot.
                if strike <= spot:
                    pe_candidates.append((spot - strike, strike, row))

        def choose(candidates):
            if not candidates:
                return None
            return min(candidates, key=lambda item: item[0])[2]

        ce = option_details(choose(ce_candidates), "CE")
        pe = option_details(choose(pe_candidates), "PE")

        return {
            "ok": bool(ce or pe),
            "symbol": symbol,
            "expiry": selected_expiry,
            "available_expiries": expiries,
            "moneyness": moneyness,
            "spot": spot,
            "atm_strike": atm_strike,
            "ce": ce,
            "pe": pe,
            "fetched_at": utc_now().isoformat(),
            "quote_timestamp_available": False,
            "quote_freshness_verified": False,
            "source": "Upstox Option Chain API",
            "message": (
                "Option-chain response received. fetched_at is server "
                "fetch time, not an exchange quote timestamp. Quote "
                "freshness is not verified; do not treat this as a trade signal."
            ),
        }

    except httpx.HTTPStatusError as exc:
        return {
            "ok": False,
            "symbol": symbol,
            "message": (
                f"Upstox returned HTTP {exc.response.status_code}. "
                "Check API access, token validity, permissions, and Render logs."
            ),
        }

    except httpx.HTTPError:
        return {
            "ok": False,
            "symbol": symbol,
            "message": "Could not connect to the Upstox option API.",
        }

    except (ValueError, TypeError, KeyError):
        return {
            "ok": False,
            "symbol": symbol,
            "message": "Unexpected or invalid option-chain response.",
        }
