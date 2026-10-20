"""FastAPI entry point. Routes are thin wrappers over app.service."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import logic
from .service import build_options, build_snapshot
from .universe import SYMBOLS
from .upstox import UpstoxClient, get_token

BASE = Path(__file__).resolve().parent.parent
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="NSE Trend Confirmation", version="1.0.0")
client = UpstoxClient()

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

NO_CACHE = {"Cache-Control": "no-store"}


def _symbol(value: str) -> str:
    symbol = value.upper()
    if symbol not in SYMBOLS:
        raise HTTPException(status_code=404, detail="Symbol NIFTY या BANKNIFTY होना चाहिए।")
    return symbol


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(BASE / "static" / "index.html", headers=NO_CACHE)


@app.get("/api/health")
async def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "token_configured": bool(get_token()),
            "time_ist": logic.now_ist().isoformat(),
            "market_open": logic.regular_market_hours(),
        },
        headers=NO_CACHE,
    )


@app.get("/api/snapshot/{symbol}")
async def snapshot(symbol: str) -> JSONResponse:
    result = await build_snapshot(client, _symbol(symbol))
    return JSONResponse(result, headers=NO_CACHE)


@app.get("/api/options/{symbol}")
async def options(
    symbol: str,
    moneyness: str = Query("ATM"),
    expiry_date: str | None = Query(None),
) -> JSONResponse:
    result = await build_options(client, _symbol(symbol), moneyness, expiry_date)
    return JSONResponse(result, headers=NO_CACHE)
