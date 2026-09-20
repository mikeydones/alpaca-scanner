"""Market cap. Alpaca does not serve fundamentals of any kind.

Mike's step 3.2 ("under $50 million market cap I will not trade it") is the one
rule the Alpaca API cannot answer. You need shares outstanding from somewhere
else and multiply by last price. Pick one and fill in the key.
"""
from __future__ import annotations

import os
from typing import Optional, Protocol

import requests


class MarketCapProvider(Protocol):
    def market_cap(self, symbol: str) -> Optional[float]: ...


class NasdaqScreenerProvider:
    """Free. Nasdaq publishes a full screener CSV with marketCap for every listed
    symbol. Download once a day, look up locally. No key, no rate limit, but it
    is a daily snapshot, not live."""
    URL = ("https://api.nasdaq.com/api/screener/stocks"
           "?tableonly=true&limit=25000&download=true")

    def __init__(self) -> None:
        self._cache: dict[str, float] = {}

    def refresh(self) -> int:
        r = requests.get(self.URL, timeout=60,
                         headers={"User-Agent": "Mozilla/5.0", "accept": "application/json"})
        r.raise_for_status()
        for row in r.json()["data"]["rows"]:
            raw = (row.get("marketCap") or "").replace(",", "").replace("$", "")
            try:
                self._cache[row["symbol"].strip()] = float(raw)
            except ValueError:
                continue
        return len(self._cache)

    def market_cap(self, symbol: str) -> Optional[float]:
        if not self._cache:
            self.refresh()
        return self._cache.get(symbol.upper()) or None


class FMPProvider:
    """financialmodelingprep.com - paid, but returns live cap plus sector and
    industry, which also covers Mike step 3.1 ('what sector or industry')."""
    def __init__(self, key: Optional[str] = None):
        self.key = key or os.environ.get("FMP_API_KEY")

    def profile(self, symbol: str) -> dict:
        r = requests.get(f"https://financialmodelingprep.com/api/v3/profile/{symbol}",
                         params={"apikey": self.key}, timeout=20)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else {}

    def market_cap(self, symbol: str) -> Optional[float]:
        return self.profile(symbol).get("mktCap")
