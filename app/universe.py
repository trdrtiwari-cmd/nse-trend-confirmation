"""Index definitions, constituents and sector indices.

Constituent lists change when NSE rebalances (March / September). Symbols that
no longer exist are skipped automatically; the advance-decline check is only
used when at least 80% of the list could be priced. Update the lists below
after each rebalance.
"""

SYMBOLS = {
    "NIFTY": {"key": "NSE_INDEX|Nifty 50", "label": "NIFTY 50"},
    "BANKNIFTY": {"key": "NSE_INDEX|Nifty Bank", "label": "BANK NIFTY"},
}

NIFTY50 = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HINDALCO",
    "HINDUNILVR", "ICICIBANK", "INDIGO", "INFY", "ITC",
    "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "MAXHEALTH", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN",
    "SUNPHARMA", "TATACONSUMER", "TATASTEEL", "TCS", "TECHM",
    "TITAN", "TMPV", "TRENT", "ULTRACEMCO", "WIPRO",
]

BANKNIFTY = [
    "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK",
    "BANKBARODA", "PNB", "CANBK", "AUBANK", "FEDERALBNK", "IDFCFIRSTB",
]

CONSTITUENTS = {"NIFTY": NIFTY50, "BANKNIFTY": BANKNIFTY}

# Display name -> Upstox index instrument key. Indices that Upstox does not
# recognise are simply skipped (at least 5 must respond for the check to count).
SECTOR_INDICES = {
    "Bank": "NSE_INDEX|Nifty Bank",
    "IT": "NSE_INDEX|Nifty IT",
    "Auto": "NSE_INDEX|Nifty Auto",
    "FMCG": "NSE_INDEX|Nifty FMCG",
    "Pharma": "NSE_INDEX|Nifty Pharma",
    "Metal": "NSE_INDEX|Nifty Metal",
    "Realty": "NSE_INDEX|Nifty Realty",
    "Energy": "NSE_INDEX|Nifty Energy",
    "Fin Services": "NSE_INDEX|Nifty Fin Service",
    "PSU Bank": "NSE_INDEX|Nifty PSU Bank",
    "Media": "NSE_INDEX|Nifty Media",
    "Pvt Bank": "NSE_INDEX|Nifty Pvt Bank",
}
