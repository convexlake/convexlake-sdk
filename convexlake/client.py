"""
Convex Lake Python SDK.

Usage:
    from convexlake import ConvexLake

    client = ConvexLake(api_key="do_XXXX...")

    # Browse available files
    info = client.info(exchange="kalshi", ticker="btc", date="2026-07-30")

    # Download candles as a DataFrame
    df = client.candles(exchange="kalshi", ticker="btc", date="2026-07-30", slug="KXBTC-2601-prices.csv")

    # Download trades as a DataFrame
    df = client.trades(exchange="polymarket", ticker="btc", date="2026-07-30", slug="...")

    # Download orderbook as a list of dicts
    msgs = client.orderbook(exchange="kalshi", ticker="btc", timeframe="1h", date="2026-07-30", slug="...")

    # Download volatility surface as a DataFrame
    df = client.surface(exchange="deribit", ticker="btc", date="2026-07-30", slug="BTC-2026-07-30.csv")
"""

from __future__ import annotations

import ast
import gzip
import io
import time
from typing import Any

import pandas as pd
import requests

from .exceptions import AuthError, ConvexLakeError, NotFoundError, PlanError, RateLimitError

BASE_URL = "https://api.convexlake.com"


class ConvexLake:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: int = 60,
        max_retries: int = 3,
    ):
        self._session = requests.Session()
        self._session.headers.update({"x-api-key": api_key})
        self._timeout = timeout
        self._max_retries = max_retries

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict) -> requests.Response:
        url = f"{BASE_URL}{path}"
        params = {k: v for k, v in params.items() if v is not None}
        delay = 2.0
        for attempt in range(self._max_retries):
            r = self._session.get(url, params=params, timeout=self._timeout)
            if r.status_code == 429:
                retry_after = float(r.headers.get("Retry-After", delay))
                if attempt < self._max_retries - 1:
                    time.sleep(retry_after)
                    delay *= 2
                    continue
                raise RateLimitError(f"Rate limit exceeded. Retry after {retry_after}s.")
            if r.status_code == 401:
                raise AuthError("Invalid or missing API key.")
            if r.status_code == 403:
                raise PlanError("This endpoint requires a higher subscription tier.")
            if r.status_code == 404:
                raise NotFoundError(f"No data found: {r.text}")
            if r.status_code == 400:
                raise ConvexLakeError(f"Bad request: {r.text}")
            r.raise_for_status()
            return r
        raise RateLimitError("Rate limit exceeded after all retries.")

    @staticmethod
    def _decompress(r: requests.Response) -> bytes:
        content = r.content
        # requests/urllib3 transparently decompresses Content-Encoding:gzip,
        # so r.content is already plain bytes in that case — don't decompress twice.
        if r.headers.get("Content-Encoding") == "gzip":
            return content
        # Raw .gz file served without Content-Encoding (just Content-Type:application/gzip).
        if content[:2] == b"\x1f\x8b":
            return gzip.decompress(content)
        return content

    @staticmethod
    def _to_df(raw: bytes) -> pd.DataFrame:
        return pd.read_csv(io.BytesIO(raw))

    @staticmethod
    def _parse_orderbook(raw: bytes, fmt: str = "auto") -> list[dict]:
        """
        Parse an orderbook file into a list of dicts.

        Handles three on-disk formats:
          "json"     → newline-delimited JSON objects (Limitless)
          "python"   → newline-delimited Python dict literals (Polymarket BBO, Predict)
          "concat"   → concatenated Python dict literals with no separator (Kalshi)
          "auto"     → detect format from content structure
        """
        import json

        text = raw.decode("utf-8")
        records: list[dict] = []

        if fmt == "auto":
            has_newlines = "\n" in text
            if not has_newlines and "}{" in text:
                fmt = "concat"
            elif has_newlines:
                first = next((l.strip() for l in text.splitlines() if l.strip()), "")
                fmt = "json" if first.startswith("{") and '"' in first[:20] else "python"
            else:
                fmt = "python"  # single-record file

        if fmt == "concat":
            parts = text.split("}{")
            for i, part in enumerate(parts):
                if i == 0:
                    s = part + "}"
                elif i == len(parts) - 1:
                    s = "{" + part
                else:
                    s = "{" + part + "}"
                try:
                    records.append(ast.literal_eval(s))
                except Exception:
                    continue
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    if fmt == "json":
                        records.append(json.loads(line))
                    else:
                        records.append(ast.literal_eval(line))
                except Exception:
                    continue

        return records

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def info(
        self,
        exchange: str | None = None,
        ticker: str | None = None,
        timeframe: str | None = None,
        date: str | None = None,
    ) -> Any:
        """
        Browse available data. Call progressively to drill down.

        Returns a dict with the listing at the current level.

        Examples:
            client.info()                                      # list exchanges
            client.info(exchange="kalshi")                     # list tickers
            client.info(exchange="kalshi", ticker="btc")       # list timeframes/dates
            client.info(exchange="kalshi", ticker="btc", date="2026-07-30")  # list files
        """
        r = self._get("/info", {
            "exchange": exchange,
            "ticker": ticker,
            "timeframe": timeframe,
            "date": date,
        })
        return r.json()

    def candles(
        self,
        exchange: str,
        slug: str,
        ticker: str | None = None,
        date: str | None = None,
        timeframe: str | None = None,
    ) -> pd.DataFrame:
        """
        Download candle data as a DataFrame.

        Columns vary by exchange:
          kalshi:    timestamp, yes_bid, yes_ask, price, volume
          polymarket: timestamp, Yes, No
          predict-fun: slug, starts_at, ends_at, market_variant, title,
                       winning_outcome, open, close, feed_symbol, outcomes
          deribit / hyperliquid: timestamp_ms, open, high, low, close, volume, cost
            — these use rolling per-instrument files; pass timeframe + slug only,
              ticker and date are not used.
        """
        r = self._get("/candles", {
            "exchange": exchange,
            "ticker": ticker,
            "timeframe": timeframe,
            "date": date,
            "slug": slug,
        })
        return self._to_df(self._decompress(r))

    def trades(
        self,
        exchange: str,
        slug: str,
        ticker: str | None = None,
        date: str | None = None,
        timeframe: str | None = None,
    ) -> pd.DataFrame:
        """
        Download trade history as a DataFrame.

        Columns vary by exchange:
          kalshi:    created_time, yes_price, no_price, count, taker_side,
                     taker_outcome_side, is_block_trade
          polymarket: timestamp, price, size, side, outcome, transaction_hash
          deribit:   timestamp_ms, trade_id, trade_seq, instrument_name,
                     direction, amount, price, mark_price, index_price, iv, liquidation
        """
        r = self._get("/trades", {
            "exchange": exchange,
            "ticker": ticker,
            "timeframe": timeframe,
            "date": date,
            "slug": slug,
        })
        return self._to_df(self._decompress(r))

    def orderbook(
        self,
        exchange: str,
        slug: str,
        ticker: str | None = None,
        date: str | None = None,
        timeframe: str | None = None,
        fmt: str = "auto",
    ) -> list[dict]:
        """
        Download orderbook data as a list of dicts.

        Each dict is one message/snapshot. Format varies by exchange:
          kalshi:    orderbook_snapshot / orderbook_delta messages
          polymarket: best_bid_ask BBO events
          predict:   full book snapshots with bids/asks arrays
          limitless: full book snapshots in JSON format
          deribit:   returns a DataFrame (CSV format) — use orderbook_df() instead

        fmt: "auto" (default), "json", or "python"
        """
        if exchange.lower() == "deribit":
            raise ConvexLakeError(
                "Deribit orderbook is CSV format — use client.orderbook_df() instead."
            )
        r = self._get("/orderbook", {
            "exchange": exchange,
            "ticker": ticker,
            "timeframe": timeframe,
            "date": date,
            "slug": slug,
        })
        raw = self._decompress(r)
        return self._parse_orderbook(raw, fmt=fmt)

    def orderbook_df(
        self,
        exchange: str,
        slug: str,
        ticker: str | None = None,
        date: str | None = None,
        timeframe: str | None = None,
    ) -> pd.DataFrame:
        """
        Download orderbook data as a DataFrame (for exchanges that use CSV format: deribit).
        """
        r = self._get("/orderbook", {
            "exchange": exchange,
            "ticker": ticker,
            "timeframe": timeframe,
            "date": date,
            "slug": slug,
        })
        return self._to_df(self._decompress(r))

    def surface(
        self,
        exchange: str,
        date: str,
        slug: str,
        ticker: str | None = None,
    ) -> pd.DataFrame:
        """
        Download volatility surface data as a DataFrame.

        Columns (deribit):
          snapshot_ts, instrument_name, expiry, strike, option_type,
          underlying_price, mark_price, mark_iv, bid_price, ask_price,
          mid_price, volume, volume_usd, open_interest, estimated_delivery_price

        slug format: "{CURRENCY}-{YYYY-MM-DD}" e.g. "BTC-2026-09-14"
        ticker is optional (not used in the S3 path).
        """
        r = self._get("/surface", {
            "exchange": exchange,
            "ticker": ticker,
            "date": date,
            "slug": slug,
        })
        return self._to_df(self._decompress(r))

    def leaderboard(
        self,
        exchange: str,
        date: str,
    ) -> pd.DataFrame:
        """
        Download the daily leaderboard as a DataFrame.

        Supported exchanges: polymarket, limitless.

        Columns (polymarket):
          fetched_at, time_period, order_by, category, rank, proxyWallet,
          userName, xUsername, verifiedBadge, vol, pnl

        Columns (limitless):
          fetched_at, metric, rank, account, username, displayName,
          positionSize_usd, unrealizedPnl_usd, heldShares, toWin_usd,
          mark_price, mark_source, side, outcomeLabel, market_id, market_title, market_category
        """
        r = self._get("/leaderboard", {
            "exchange": exchange,
            "date": date,
        })
        return self._to_df(self._decompress(r))

    def leaderboard_trades(
        self,
        exchange: str,
        address: str | None = None,
    ) -> Any:
        """
        Two-step leaderboard-trades access.

        Step 1 — list traders (no address):
            Returns a dict with an "addresses" key containing up to 1000 trader addresses.
            client.leaderboard_trades(exchange="polymarket")

        Step 2 — fetch one trader's fills (with address):
            Returns a DataFrame with per-trader fill history.
            client.leaderboard_trades(exchange="polymarket", address="0x...")

        Supported exchanges: polymarket, limitless.

        Columns (polymarket step 2):
          proxyWallet, side, asset, conditionId, size, price, timestamp,
          title, slug, outcome, outcomeIndex, name, transactionHash
        """
        r = self._get("/leaderboard-trades", {
            "exchange": exchange,
            "address": address,
        })
        if address is None:
            return r.json()
        return self._to_df(self._decompress(r))
