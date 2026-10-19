from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

BASE = Path(__file__).resolve().parent.parent
app = FastAPI(title="NSE Trend Confirmation", version="0.1.0")
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

REQUIRED = ["advance_decline", "sector_heatmap", "option_premium_change", "call_put_graph", "index_direction"]

class DataProvider:
    """Plug a permitted, licensed or user-authorized data source in here.

    Do not scrape NSE pages. NSE's published data policy governs data use;
    free public pages are not a guaranteed real-time API.
    """
    async def snapshot(self, symbol: str) -> dict[str, Any]:
        # Intentionally returns missing data until a legitimate provider is configured.
        # This guarantees the app never fabricates prices or trading signals.
        return {
            "symbol": symbol,
            "as_of": None,
            "source": "Not configured",
            "data": {key: None for key in REQUIRED},
            "message": "No authorized live data provider is configured. Signal suppressed.",
        }

provider = DataProvider()

@app.get("/")
def home():
    return FileResponse(BASE / "static" / "index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "app": app.title, "time": datetime.now(timezone.utc).isoformat()}

@app.get("/api/snapshot/{symbol}")
async def snapshot(symbol: str):
    symbol = symbol.upper()
    if symbol not in {"NIFTY", "BANKNIFTY"}:
        return {"symbol": symbol, "signal": "WAIT", "ready": False, "message": "Unsupported symbol. Choose NIFTY or BANKNIFTY."}
    snap = await provider.snapshot(symbol)
    data = snap.get("data") or {}
    missing = [key for key in REQUIRED if data.get(key) is None]
    ready = not missing and bool(snap.get("as_of"))
    signal = "WAIT"
    reasons = []
    if not ready:
        reasons.append("Required data is missing or timestamp is unavailable; no signal generated.")
    else:
        # Conservative transparent rule-based starter; provider must supply normalized fields.
        # Expected fields: advance_decline.ratio, sector_heatmap.bias (-1/0/1),
        # option_premium_change.ce_pct/pe_pct, call_put_graph.bias (-1/0/1),
        # index_direction.bias (-1/0/1), all measured over a common interval.
        try:
            d = data
            breadth = float(d["advance_decline"]["ratio"])
            sector = int(d["sector_heatmap"]["bias"])
            ce = float(d["option_premium_change"]["ce_pct"])
            pe = float(d["option_premium_change"]["pe_pct"])
            graph = int(d["call_put_graph"]["bias"])
            trend = int(d["index_direction"]["bias"])
            bull = breadth > 1.1 and sector == 1 and ce > pe and graph == 1 and trend == 1
            bear = breadth < (1 / 1.1) and sector == -1 and pe > ce and graph == -1 and trend == -1
            if bull:
                signal = "BUY CE"
            elif bear:
                signal = "BUY PE"
            else:
                signal = "WAIT"
            reasons.append("Signal rule: all five checks must align; otherwise WAIT.")
        except (KeyError, TypeError, ValueError):
            ready = False
            signal = "WAIT"
            reasons.append("Provider data did not match the required schema; signal suppressed.")
    return {
        "symbol": symbol, "ready": ready, "signal": signal,
        "as_of": snap.get("as_of"), "source": snap.get("source", "Unknown"),
        "data": data, "missing": missing, "message": snap.get("message"),
        "reasons": reasons,
    }

