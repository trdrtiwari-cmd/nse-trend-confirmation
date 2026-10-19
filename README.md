# NSE Trend Confirmation — Independent Website

A standalone Hindi-first dashboard for NIFTY 50 and BANK NIFTY. It does not depend on `trader-tiwari.onrender.com`, `trader-tiwari-2.onrender.com`, Groww, or any previous project.

## Current state
- Dashboard and FastAPI backend are included.
- Supports NIFTY and BANKNIFTY selection.
- Tracks the five intended checks: Advance–Decline, Sector Heat Map, CE/PE premium % change, CE/PE premium graph bias, and index direction.
- Final output is `BUY CE`, `BUY PE`, or `WAIT`.
- **No provider is configured yet.** Missing data deliberately produces `WAIT`; the app never invents prices or signals.

## Run locally (Windows)
Install Python 3.10+ first. Open Command Prompt in this folder:

```bat
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

## Deploy as a separate Render service
1. Create a new, separate GitHub repository (do not reuse the old bridge/API repository).
2. Upload the contents of this folder to the repository root.
3. In Render, choose **New + → Blueprint** and connect that repository. Render reads `render.yaml`.
4. After deployment, open the new service URL and check `/api/health`.
5. The dashboard will still show `WAIT` until a permitted data provider is connected.

The free hosting plan may sleep or have resource limits; it does not make market-data access free.

## Live data requirements
A separate data adapter must be implemented in `app/main.py` (or a new provider module) using an API whose terms permit the intended use. Required normalized fields are described below. Do not scrape NSE pages or assume public web pages are a stable real-time API. Review NSE data licensing and provider terms.

Expected snapshot shape:

```json
{
  "symbol": "NIFTY",
  "as_of": "2026-10-19T10:15:00+05:30",
  "source": "Authorized provider name",
  "data": {
    "advance_decline": {"ratio": 1.35},
    "sector_heatmap": {"bias": 1},
    "option_premium_change": {"ce_pct": 8.2, "pe_pct": -3.1},
    "call_put_graph": {"bias": 1},
    "index_direction": {"bias": 1}
  },
  "message": "Provider snapshot"
}
```

`bias`: `1` bullish, `0` neutral, `-1` bearish. The provider must calculate comparable values from a consistent time window and supply timestamps. Add stale-data checks before enabling live signals.

## Signal policy
The starter rule only emits a directional signal when all five checks agree. Otherwise it returns `WAIT`. This is a transparent prototype, not a proven profitable strategy. Test with historical data and paper trading before risking money.

## Endpoints
- `GET /` — dashboard
- `GET /api/health` — health check
- `GET /api/snapshot/NIFTY`
- `GET /api/snapshot/BANKNIFTY`

## Deployment note
Render service definition is in `render.yaml`. A Dockerfile is also included for other compatible hosts.
