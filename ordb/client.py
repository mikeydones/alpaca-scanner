"""Thin Alpaca REST wrapper. Only the endpoints this strategy actually needs."""
from __future__ import annotations

import os, time
from typing import Iterable, Optional

import pandas as pd
import requests

DATA = "https://data.alpaca.markets"
PAPER = "https://paper-api.alpaca.markets"
LIVE = "https://api.alpaca.markets"


class Alpaca:
    def __init__(self, key: Optional[str] = None, secret: Optional[str] = None, paper: bool = True):
        self.key = key or os.environ["ALPACA_KEY_ID"]
        self.secret = secret or os.environ["ALPACA_SECRET_KEY"]
        self.trading = PAPER if paper else LIVE
        self.s = requests.Session()
        self.s.headers.update({
            "APCA-API-KEY-ID": self.key,
            "APCA-API-SECRET-KEY": self.secret,
            "accept": "application/json",
        })

    # ---------------- plumbing ----------------
    def _get(self, url: str, **params) -> dict:
        for attempt in range(5):
            r = self.s.get(url, params={k: v for k, v in params.items() if v is not None}, timeout=30)
            if r.status_code == 429:                      # 200/min basic, 10k/min ATP
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def _paged(self, url: str, key: str, **params):
        token = None
        while True:
            body = self._get(url, **params, page_token=token)
            yield body.get(key) or {}
            token = body.get("next_page_token")
            if not token:
                return

    # ---------------- trading ----------------
    def account(self) -> dict:
        return self._get(f"{self.trading}/v2/account")

    def assets(self, status="active", asset_class="us_equity") -> list[dict]:
        """GET /v2/assets - the Trading API one, NOT broker-api/v1/assets."""
        return self._get(f"{self.trading}/v2/assets", status=status, asset_class=asset_class)

    def tradable_universe(self, exchanges=("NASDAQ", "NYSE", "ARCA", "AMEX")) -> list[dict]:
        return [a for a in self.assets()
                if a.get("tradable") and a.get("exchange") in exchanges
                and a.get("class") == "us_equity"]

    def submit_order(self, payload: dict) -> dict:
        r = self.s.post(f"{self.trading}/v2/orders", json=payload, timeout=30)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id: str) -> None:
        self.s.delete(f"{self.trading}/v2/orders/{order_id}", timeout=30)

    def positions(self) -> list[dict]:
        return self._get(f"{self.trading}/v2/positions")

    # ---------------- market data ----------------
    def snapshots(self, symbols: Iterable[str], feed="sip") -> dict:
        """One call for latest trade+quote, minute bar, daily bar and PREV daily bar.

        This is the cheap first pass. Batch ~200 symbols per request.
        """
        out: dict = {}
        syms = list(symbols)
        for i in range(0, len(syms), 200):
            chunk = ",".join(syms[i:i + 200])
            out.update(self._get(f"{DATA}/v2/stocks/snapshots", symbols=chunk, feed=feed))
        return out

    def bars(self, symbols: Iterable[str], timeframe: str, start: str, end: Optional[str] = None,
             feed="sip", adjustment="all", limit=10000) -> dict[str, pd.DataFrame]:
        """GET /v2/stocks/bars, paginated, returned as per-symbol DataFrames.

        timeframe: [1-59]Min | [1-23]Hour | 1Day | 1Week | [1,2,3,4,6,12]Month
        """
        acc: dict[str, list] = {}
        syms = list(symbols)
        for i in range(0, len(syms), 100):
            chunk = ",".join(syms[i:i + 100])
            for page in self._paged(f"{DATA}/v2/stocks/bars", "bars", symbols=chunk,
                                    timeframe=timeframe, start=start, end=end,
                                    feed=feed, adjustment=adjustment, limit=limit):
                for sym, rows in page.items():
                    acc.setdefault(sym, []).extend(rows)
        return {sym: _to_frame(rows) for sym, rows in acc.items()}

    def most_actives(self, by="volume", top=100) -> list[dict]:
        body = self._get(f"{DATA}/v1beta1/screener/stocks/most-actives", by=by, top=top)
        return body.get("most_actives", [])

    def movers(self, top=50) -> dict:
        return self._get(f"{DATA}/v1beta1/screener/stocks/movers", top=top)

    def news(self, symbols: Iterable[str], start: str, limit=50) -> list[dict]:
        """Benzinga-sourced headlines - covers Mike step 3 ('read what catalyst')."""
        body = self._get(f"{DATA}/v1beta1/news", symbols=",".join(symbols),
                         start=start, limit=limit, sort="desc")
        return body.get("news", [])


def _to_frame(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df = df.rename(columns={"t": "ts", "o": "open", "h": "high", "l": "low",
                            "c": "close", "v": "volume", "n": "trades", "vw": "vwap"})
    df["ts"] = pd.to_datetime(df["ts"], utc=True, format="ISO8601")
    return df.set_index("ts").sort_index()
