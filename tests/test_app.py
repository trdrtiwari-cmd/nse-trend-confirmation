"""Offline tests: run with  python -m unittest discover -s tests -v
No network, no FastAPI needed (routes are thin wrappers over app.service)."""
import asyncio
import gzip
import json
import unittest
from datetime import datetime, timedelta
from urllib.parse import unquote

from app import logic
from app.service import build_options, build_snapshot
from app.universe import CONSTITUENTS, SECTOR_INDICES
from app.upstox import UpstoxClient, UpstoxError, _extract_equity_keys

IST = logic.IST
INDEX = "NSE_INDEX|Nifty 50"
CE_KEY, PE_KEY = "NSE_FO|51348", "NSE_FO|51349"


def candles(day: datetime, start_h, start_m, closes, first_open=None):
    """Newest-first rows like the real API."""
    rows, t, prev = [], day.replace(hour=start_h, minute=start_m, second=0, microsecond=0), first_open or closes[0]
    for c in closes:
        rows.append([t.isoformat(), prev, max(prev, c), min(prev, c), c, 1000, 0])
        prev, t = c, t + timedelta(minutes=5)
    return rows[::-1]


def chain_rows(spot=22888.95, ce=(169.9, 323.3), pe=(172.5, 108.3)):
    rows = []
    for strike in range(22700, 23101, 50):
        rows.append({
            "expiry": "2026-10-27", "strike_price": strike, "underlying_spot_price": spot,
            "call_options": {"instrument_key": CE_KEY if strike == 22900 else f"NSE_FO|c{strike}",
                             "market_data": {"ltp": ce[0], "close_price": ce[1], "volume": 17225975, "oi": 4899765, "prev_oi": 1754740},
                             "option_greeks": {"iv": 12.25, "delta": .5084, "theta": -10.36, "gamma": .001, "vega": 13.52}},
            "put_options": {"instrument_key": PE_KEY if strike == 22900 else f"NSE_FO|p{strike}",
                            "market_data": {"ltp": pe[0], "close_price": pe[1], "volume": 23331230, "oi": 3879785, "prev_oi": 2414750},
                            "option_greeks": {"iv": 12.87, "delta": -.4916, "theta": -10.88, "gamma": .0009, "vega": 13.52}},
        })
    return rows


class FakeClient(UpstoxClient):
    def __init__(self, now, index_closes, ad_up=True, sectors_up=True, ce_closes=None, pe_closes=None,
                 token="x", fail=None, index_day=None, ce_ltp=(169.9, 323.3), pe_ltp=(172.5, 108.3)):
        super().__init__(token=token)
        self.now, self.index_closes, self.ad_up, self.sectors_up = now, index_closes, ad_up, sectors_up
        self.ce_closes = ce_closes or [100, 105, 110, 118, 125, 131, 140]
        self.pe_closes = pe_closes or [100, 96, 92, 88, 84, 80, 75]
        self.fail, self.index_day = fail, index_day or now
        self.ce_ltp, self.pe_ltp = ce_ltp, pe_ltp

    async def equity_keys(self, symbols):
        return {s: f"NSE_EQ|INE_{s}" for s in symbols}

    async def _fetch(self, url, params, auth=True):
        if self.fail:
            raise self.fail
        path = url.split("api.upstox.com")[1]
        if "/intraday/" in path:
            key = unquote(path.split("/intraday/")[1].split("/minutes")[0])
            if key == INDEX:
                return {"status": "success", "data": {"candles": candles(self.index_day, 9, 15, self.index_closes)}}
            closes = self.ce_closes if key == CE_KEY else self.pe_closes
            return {"status": "success", "data": {"candles": candles(self.index_day, 9, 15, closes)}}
        if "/historical-candle/" in path:
            return {"status": "success", "data": {"candles": []}}
        if path == "/v3/market-quote/ltp":
            data, up = {}, None
            for i, key in enumerate(params["instrument_key"].split(",")):
                up = self.sectors_up if key.startswith("NSE_INDEX") else (self.ad_up if i % 5 else not self.ad_up)
                data[key.replace("|", ":", 1)] = {"instrument_token": key, "last_price": 101 if up else 99, "cp": 100}
            return {"status": "success", "data": data}
        if path == "/v2/option/contract":
            return {"status": "success", "data": [{"expiry": e} for e in ("2026-10-19", "2026-10-27", "2026-11-03")]}
        if path == "/v2/option/chain":
            return {"status": "success", "data": chain_rows(ce=self.ce_ltp, pe=self.pe_ltp)}
        raise AssertionError("unexpected " + path)


def run(coro):
    return asyncio.run(coro)


LIVE = datetime(2026, 10, 19, 10, 32, tzinfo=IST)       # Monday, market open
CLOSED = datetime(2026, 10, 19, 23, 17, tzinfo=IST)      # the user's screenshot


class Snapshot(unittest.TestCase):
    up = [22800 + i * 8 for i in range(14)]               # 09:15 .. 10:20, rising

    def test_all_bullish_live_gives_buy_ce(self):
        r = run(build_snapshot(FakeClient(LIVE, self.up, ce_ltp=(200, 170), pe_ltp=(90, 110)), "NIFTY", LIVE))
        self.assertEqual(r["market_data_status"], "RECEIVED")
        self.assertEqual(r["signal"], "BUY CE", r["reasons"])
        self.assertTrue(r["ready"])
        self.assertTrue(all(r["data"][k] for k in logic.REQUIRED))

    def test_all_bearish_live_gives_buy_pe(self):
        c = FakeClient(LIVE, self.up[::-1], ad_up=False, sectors_up=False,
                       ce_closes=[100, 96, 92, 88, 84, 80, 75], pe_closes=[100, 105, 110, 118, 125, 131, 140],
                       ce_ltp=(90, 110), pe_ltp=(200, 170))
        r = run(build_snapshot(c, "NIFTY", LIVE))
        self.assertEqual(r["signal"], "BUY PE", r["reasons"])

    def test_disagreement_waits(self):
        r = run(build_snapshot(FakeClient(LIVE, self.up, sectors_up=False), "NIFTY", LIVE))
        self.assertEqual(r["signal"], "WAIT")
        self.assertTrue(r["ready"])

    def test_market_closed_never_signals(self):
        c = FakeClient(CLOSED, self.up, index_day=CLOSED)
        r = run(build_snapshot(c, "NIFTY", CLOSED))
        self.assertEqual(r["market_data_status"], "MARKET_CLOSED")
        self.assertEqual(r["signal"], "WAIT")
        self.assertFalse(r["ready"])
        self.assertIn("Market closed", r["message"])
        self.assertIsNotNone(r["data"]["advance_decline"])      # still shown for information

    def test_stale_candle_during_session_waits(self):
        stale = LIVE.replace(hour=11, minute=5)                  # last candle ~10:20 -> 45 min old
        r = run(build_snapshot(FakeClient(stale, self.up, index_day=stale), "NIFTY", stale))
        self.assertEqual(r["market_data_status"], "STALE")
        self.assertEqual(r["signal"], "WAIT")

    def test_old_day_candles_are_stale(self):
        old = LIVE - timedelta(days=3)
        r = run(build_snapshot(FakeClient(LIVE, self.up, index_day=old), "NIFTY", LIVE))
        self.assertEqual(r["market_data_status"], "STALE")

    def test_missing_token(self):
        r = run(build_snapshot(FakeClient(LIVE, self.up, token=""), "NIFTY", LIVE))
        self.assertEqual((r["market_data_status"], r["signal"]), ("NOT_CONFIGURED", "WAIT"))

    def test_expired_token(self):
        c = FakeClient(LIVE, self.up, fail=UpstoxError("API_ERROR", "expired", 401))
        r = run(build_snapshot(c, "NIFTY", LIVE))
        self.assertEqual(r["market_data_status"], "API_ERROR")

    def test_connection_error(self):
        c = FakeClient(LIVE, self.up, fail=UpstoxError("CONNECTION_ERROR", "down"))
        self.assertEqual(run(build_snapshot(c, "BANKNIFTY", LIVE))["market_data_status"], "CONNECTION_ERROR")

    def test_one_failing_check_does_not_break_snapshot(self):
        c = FakeClient(LIVE, self.up)
        async def boom(*a, **k):
            raise UpstoxError("API_ERROR", "sector api down")
        orig = c.ltp
        async def ltp(keys):
            if keys[0].startswith("NSE_INDEX"):
                await boom()
            return await orig(keys)
        c.ltp = ltp
        r = run(build_snapshot(c, "NIFTY", LIVE))
        self.assertIsNone(r["data"]["sector_heatmap"])
        self.assertEqual(r["signal"], "WAIT")
        self.assertTrue(any("Sector" in x for x in r["reasons"]))


class Options(unittest.TestCase):
    def opts(self, **kw):
        return run(build_options(FakeClient(CLOSED, [1, 2, 3]), "NIFTY", now=CLOSED, **kw))

    def test_atm(self):
        r = self.opts(moneyness="ATM")
        self.assertTrue(r["ok"])
        self.assertEqual((r["atm_strike"], r["ce"]["strike"], r["pe"]["strike"]), (22900, 22900, 22900))
        self.assertEqual(r["expiry"], "2026-10-27")
        self.assertNotIn("2026-10-19", r["available_expiries"])   # today's expiry hidden after 15:30
        self.assertEqual(r["ce"]["instrument_key"], CE_KEY)

    def test_itm_otm_strikes(self):
        itm, otm = self.opts(moneyness="ITM"), self.opts(moneyness="OTM")
        self.assertEqual((itm["ce"]["strike"], itm["pe"]["strike"]), (22850, 22950))
        self.assertEqual((otm["ce"]["strike"], otm["pe"]["strike"]), (22950, 22850))

    def test_bad_inputs(self):
        self.assertFalse(self.opts(moneyness="XYZ")["ok"])
        self.assertFalse(self.opts(expiry_date="27-10-2026")["ok"])

    def test_unknown_expiry_falls_back_to_nearest(self):
        self.assertEqual(self.opts(expiry_date="2030-01-01")["expiry"], "2026-10-27")

    def test_today_expiry_available_during_session(self):
        r = run(build_options(FakeClient(LIVE, [1, 2, 3]), "NIFTY", now=LIVE))
        self.assertEqual(r["available_expiries"][0], "2026-10-19")


class Logic(unittest.TestCase):
    def test_trading_day_rules(self):
        sat = datetime(2026, 10, 24, 12, 0, tzinfo=IST)
        mon_early = datetime(2026, 10, 19, 8, 0, tzinfo=IST)
        self.assertEqual(logic.last_trading_day(sat).isoformat(), "2026-10-23")
        self.assertEqual(logic.last_trading_day(mon_early).isoformat(), "2026-10-16")
        self.assertFalse(logic.regular_market_hours(sat))

    def test_future_candle_is_stale(self):
        status, fresh, _ = logic.candle_status(LIVE + timedelta(hours=1), LIVE)
        self.assertEqual((status, fresh), ("STALE", False))

    def test_timestamps(self):
        self.assertEqual(logic.parse_timestamp("2026-10-19T15:25:00+05:30").hour, 15)
        self.assertEqual(logic.parse_timestamp(1792402500000).tzinfo, IST)

    def test_garbage_candles_ignored(self):
        self.assertEqual(logic.parse_candles([["bad"], [], None, ["2026-10-19T09:15:00+05:30", None, 1, 1, 1]]), [])

    def test_ad_needs_coverage(self):
        self.assertIsNone(logic.advance_decline([(101, 100)] * 3, expected=50))
        self.assertEqual(logic.advance_decline([(101, 100)] * 50, expected=50)["bias"], 1)

    def test_premium_matches_screenshot(self):
        p = logic.premium_change({"ltp": 169.9, "close_price": 323.3}, {"ltp": 172.5, "close_price": 108.3})
        self.assertEqual(p["bias"], -1)
        self.assertAlmostEqual(p["ce_pct"], -47.45, 1)

    def test_equity_key_extraction(self):
        rows = [{"segment": "NSE_EQ", "instrument_type": "EQ", "trading_symbol": "TCS", "instrument_key": "NSE_EQ|INE467B01029"},
                {"segment": "NSE_EQ", "instrument_type": "BE", "trading_symbol": "TCS", "instrument_key": "bad"},
                {"segment": "BSE_EQ", "instrument_type": "EQ", "trading_symbol": "TCS", "instrument_key": "bad"}]
        got = _extract_equity_keys(gzip.compress(json.dumps(rows).encode()), {"TCS"})
        self.assertEqual(got, {"TCS": "NSE_EQ|INE467B01029"})
        self.assertEqual(_extract_equity_keys(b"junk", {"TCS"}), {})

    def test_universe_sizes(self):
        self.assertEqual(len(CONSTITUENTS["NIFTY"]), 50)
        self.assertGreaterEqual(len(SECTOR_INDICES), 8)


if __name__ == "__main__":
    unittest.main()
