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

    # Download orderbook and convert to NautilusTrader format
    df = client.download_nautilus(
        exchange="polymarket", slug="btc-updown-15m-1776943800.gz",
        instrument_id="BTC.POLYMARKET", ticker="btc", timeframe="15m", date="2026-04-23",
    )

    # Or convert already-fetched messages
    msgs = client.orderbook(exchange="limitless", slug="...", ticker="btc", date="2026-07-30")
    df = ConvexLake.to_nautilus(msgs, instrument_id="BTC.LIMITLESS")
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

# NautilusTrader BookAction values
_ACTION_ADD    = 1
_ACTION_UPDATE = 2
_ACTION_DELETE = 3
_ACTION_CLEAR  = 4

# NautilusTrader OrderSide values
_SIDE_BUY  = 1
_SIDE_SELL = 2
_SIDE_NONE = 0

# RecordFlag.F_LAST — marks last delta in a snapshot sequence
_FLAG_LAST = 1

_TS_FIELDS = ("local_timestamp", "timestamp", "ts", "time", "created_at")


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
          "json"     → newline-delimited JSON objects (Limitless, Hyperliquid, Binance, Deribit perps, Opinion)
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

    @staticmethod
    def _get_ts_ns(msg: dict) -> int:
        """Extract timestamp from a message dict and return nanoseconds.

        Epoch ranges (2001–2286):
          ms  → 1e12–1e13   detect: v < 1e14
          µs  → 1e15–1e16   detect: 1e14 ≤ v < 1e17
          ns  → 1e18–1e19   detect: v ≥ 1e17
        """
        for field in _TS_FIELDS:
            v = msg.get(field)
            if v is not None:
                v = int(v)
                if v >= 100_000_000_000_000_000:   # ≥ 1e17 → nanoseconds
                    return v
                elif v >= 100_000_000_000_000:     # ≥ 1e14 → microseconds
                    return v * 1_000
                else:                               # < 1e14 → milliseconds
                    return v * 1_000_000
        return 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def info(
        self,
        exchange: str | None = None,
        ticker: str | None = None,
        market: str | None = None,
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
            "market": market,
            "timeframe": timeframe,
            "date": date,
        })
        return r.json()

    def candles(
        self,
        exchange: str,
        slug: str,
        ticker: str | None = None,
        market: str | None = None,
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
            "market": market,
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
        market: str | None = None,
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
            "market": market,
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
        market: str | None = None,
        date: str | None = None,
        timeframe: str | None = None,
        md_type: str | None = None,
        fmt: str = "auto",
    ) -> list[dict]:
        """
        Download orderbook data as a list of dicts.

        Each dict is one message/snapshot. Format varies by exchange:
          kalshi:    orderbook_snapshot / orderbook_delta messages (concat Python format)
          polymarket: BBO events or full snapshots (Python dict format)
          predict:   full book snapshots with bids/asks arrays
          limitless: full book snapshots in JSON format
          hyperliquid, binance, deribit (perps), bitfinex, opinion: JSON snapshots
          deribit (options): CSV format — use orderbook_df() instead

        md_type: optional filter, e.g. "bbo" for best-bid/ask only files (Polymarket).
        fmt: "auto" (default), "json", or "python"
        """
        r = self._get("/orderbook", {
            "exchange": exchange,
            "ticker": ticker,
            "market": market,
            "timeframe": timeframe,
            "date": date,
            "slug": slug,
            "md_type": md_type,
        })
        raw = self._decompress(r)
        return self._parse_orderbook(raw, fmt=fmt)

    def orderbook_df(
        self,
        exchange: str,
        slug: str,
        ticker: str | None = None,
        market: str | None = None,
        date: str | None = None,
        timeframe: str | None = None,
    ) -> pd.DataFrame:
        """
        Download orderbook data as a DataFrame (for CSV-format files: Deribit options).
        """
        r = self._get("/orderbook", {
            "exchange": exchange,
            "ticker": ticker,
            "market": market,
            "timeframe": timeframe,
            "date": date,
            "slug": slug,
        })
        return self._to_df(self._decompress(r))

    def orderbook_weather(
        self,
        exchange: str,
        market: str,
        date: str,
        slug: str,
        *,
        presign: bool = False,
        expires: int | None = None,
    ) -> bytes | dict:
        """
        Download a weather orderbook file.

        Parameters
        ----------
        exchange : str
        market : str
            Weather market identifier (e.g. city or station code).
        date : str
            Date in YYYY-MM-DD format.
        slug : str
            File name within the date folder.
        presign : bool
            If True, returns a JSON dict with a pre-signed download URL instead of raw bytes.
        expires : int, optional
            Pre-signed URL lifetime in seconds (60–3600, default 900). Only used when presign=True.
        """
        params: dict = {
            "exchange": exchange,
            "market": market,
            "date": date,
            "slug": slug,
        }
        if presign:
            params["presign"] = "1"
        if expires is not None:
            params["expires"] = str(expires)
        r = self._get("/orderbook-weather", params)
        if presign:
            return r.json()
        return self._decompress(r)

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

        Supported exchanges: polymarket, limitless, predict-fun.

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

    # ------------------------------------------------------------------
    # NautilusTrader integration
    # ------------------------------------------------------------------

    @staticmethod
    def to_nautilus(
        messages: list[dict],
        instrument_id: str,
        *,
        price_precision: int = 4,
        size_precision: int = 2,
    ) -> pd.DataFrame:
        """
        Convert orderbook messages to a NautilusTrader-compatible OrderBookDelta DataFrame.

        Each snapshot expands into a CLEAR action followed by ADD actions for all bid and
        ask levels. The final ADD in each snapshot sequence carries flags=1 (F_LAST) to
        signal that the book is complete at that point. BBO messages (best bid/offer only)
        expand to CLEAR + two ADD actions. Kalshi delta messages map to ADD/UPDATE/DELETE.

        Supported message formats
        -------------------------
        Snapshot (Polymarket full book, Limitless, Predict, Hyperliquid, Binance,
        Deribit perps, Bitfinex, Opinion, HIP-4):
            {"type": "snapshot", "bids": [[price, size], ...], "asks": [[...], ...],
             "local_timestamp": <ms>}

        BBO (Polymarket BBO files, Opinion BBO):
            {"type": "bbo", "bid": price, "bid_size": size, "ask": price, "ask_size": size,
             "local_timestamp": <ms>}
            {"best_bid": price, "best_ask": price, "timestamp": <ms>}

        Kalshi snapshot:
            {"type": "orderbook_snapshot",
             "msg": {"yes_dollars_fp": [[price_cents, size], ...],
                     "no_dollars_fp": [[price_cents, size], ...]}}

        Kalshi delta:
            {"type": "orderbook_delta",
             "msg": {"side": "yes"|"no", "price": <cents>, "delta": <int>}}

        Parameters
        ----------
        messages : list[dict]
            Raw messages from client.orderbook().
        instrument_id : str
            NautilusTrader instrument identifier, e.g. "TRUMP.POLYMARKET" or
            "BTC-PERP.BINANCE".
        price_precision : int
            Decimal places for fixed-point price encoding (default 4).
            Prediction markets: 4 (prices 0–1, e.g. 0.8400 → 8400).
            Perpetuals: 1 (e.g. 65000.5 → 650005).
        size_precision : int
            Decimal places for fixed-point size encoding (default 2).

        Returns
        -------
        pd.DataFrame
            Columns: instrument_id, action, side, price, size, order_id, flags,
            sequence, ts_event, ts_init.
            Compatible with NautilusTrader's ParquetDataCatalog after converting
            each row to an OrderBookDelta object.

        Example — write to NautilusTrader catalog
        ------------------------------------------
            from nautilus_trader.model.data import OrderBookDelta
            from nautilus_trader.persistence.catalog import ParquetDataCatalog

            df = ConvexLake.to_nautilus(msgs, "TRUMP.POLYMARKET")
            # build OrderBookDelta objects from df rows, then:
            catalog = ParquetDataCatalog("./catalog")
            catalog.write_data(deltas)
        """
        p_mult = 10 ** price_precision
        s_mult = 10 ** size_precision

        rows: list[dict] = []
        seq: int = 0

        def _row(action: int, side: int, price: float, size: float,
                 ts_ns: int, flags: int = 0) -> dict:
            nonlocal seq
            seq += 1
            return {
                "instrument_id": instrument_id,
                "action":        action,
                "side":          side,
                "price":         round(float(price) * p_mult),
                "size":          round(float(size)  * s_mult),
                "order_id":      0,
                "flags":         flags,
                "sequence":      seq,
                "ts_event":      ts_ns,
                "ts_init":       ts_ns,
            }

        def _parse_level(level: list) -> tuple[float, float]:
            # Bitfinex stores [price, count, amount]; all others store [price, size]
            if len(level) == 3:
                return float(level[0]), abs(float(level[2]))
            return float(level[0]), float(level[1])

        for msg in messages:
            ts_ns  = ConvexLake._get_ts_ns(msg)
            mtype  = msg.get("type", "")

            # ── Standard snapshot (most venues, including Bitfinex) ────
            # Excludes "update" (Bitfinex/Deribit deltas) and "bbo" messages.
            if ("bids" in msg or "asks" in msg) and mtype not in ("bbo", "update"):
                bids = msg.get("bids", [])
                asks = msg.get("asks", [])
                rows.append(_row(_ACTION_CLEAR, _SIDE_NONE, 0, 0, ts_ns))
                for level in bids:
                    p, s = _parse_level(level)
                    rows.append(_row(_ACTION_ADD, _SIDE_BUY, p, s, ts_ns))
                for level in asks:
                    p, s = _parse_level(level)
                    rows.append(_row(_ACTION_ADD, _SIDE_SELL, p, s, ts_ns))
                if rows:
                    rows[-1]["flags"] = _FLAG_LAST

            # ── BBO (bid/ask top-of-book only) ─────────────────────────
            elif mtype == "bbo" or "best_bid" in msg or ("bid" in msg and "ask" in msg
                                                         and "bids" not in msg):
                if mtype == "bbo":
                    bp, bs = msg.get("bid"),      msg.get("bid_size", 0)
                    ap, as_ = msg.get("ask"),     msg.get("ask_size", 0)
                else:
                    bp, bs  = msg.get("best_bid"), msg.get("bid_size", 1)
                    ap, as_ = msg.get("best_ask"), msg.get("ask_size", 1)
                if bp is not None and ap is not None:
                    rows.append(_row(_ACTION_CLEAR, _SIDE_NONE, 0, 0, ts_ns))
                    rows.append(_row(_ACTION_ADD, _SIDE_BUY,  bp, bs,  ts_ns))
                    rows.append(_row(_ACTION_ADD, _SIDE_SELL, ap, as_, ts_ns, flags=_FLAG_LAST))

            # ── Deribit L2 delta ───────────────────────────────────────
            # {"type":"update","side":"bid"|"ask","action":"new"|"change"|"delete",
            #  "price":p,"size":s,"local_timestamp":ms}
            elif mtype == "update" and "side" in msg and "action" in msg:
                side_str   = msg["side"]   # "bid" or "ask"
                action_str = msg["action"] # "new", "change", "delete"
                price      = float(msg.get("price", 0))
                size       = float(msg.get("size",  0))
                side       = _SIDE_BUY if side_str == "bid" else _SIDE_SELL
                if action_str == "new":
                    action = _ACTION_ADD
                elif action_str == "change":
                    action = _ACTION_UPDATE
                else:  # "delete"
                    action = _ACTION_DELETE
                    size   = 0
                rows.append(_row(action, side, price, size, ts_ns, flags=_FLAG_LAST))

            # ── Bitfinex L2 delta ──────────────────────────────────────
            # {"type":"update","price":p,"count":c,"amount":a,"local_timestamp":ms}
            # count==0 → delete level (amount sign encodes side); count>0 → add/update
            elif mtype == "update" and "count" in msg and "amount" in msg:
                price  = float(msg["price"])
                count  = int(msg["count"])
                amount = float(msg["amount"])
                side   = _SIDE_BUY if amount >= 0 else _SIDE_SELL
                if count == 0:
                    action = _ACTION_DELETE
                    size   = 0
                else:
                    action = _ACTION_ADD
                    size   = abs(amount)
                rows.append(_row(action, side, price, size, ts_ns, flags=_FLAG_LAST))

            # ── Kalshi orderbook_snapshot ──────────────────────────────
            elif mtype == "orderbook_snapshot" and "msg" in msg:
                inner = msg["msg"]
                yes_levels = inner.get("yes_dollars_fp", [])
                no_levels  = inner.get("no_dollars_fp",  [])
                rows.append(_row(_ACTION_CLEAR, _SIDE_NONE, 0, 0, ts_ns))
                for lv in yes_levels:
                    rows.append(_row(_ACTION_ADD, _SIDE_BUY,  lv[0] / 100, lv[1], ts_ns))
                for lv in no_levels:
                    rows.append(_row(_ACTION_ADD, _SIDE_SELL, lv[0] / 100, lv[1], ts_ns))
                if rows:
                    rows[-1]["flags"] = _FLAG_LAST

            # ── Kalshi orderbook_delta ─────────────────────────────────
            elif mtype == "orderbook_delta" and "msg" in msg:
                inner  = msg["msg"]
                side   = _SIDE_BUY if inner.get("side") == "yes" else _SIDE_SELL
                price  = inner.get("price", 0) / 100
                delta  = inner.get("delta", 0)
                if delta == 0:
                    action = _ACTION_DELETE
                    size   = 0
                elif delta > 0:
                    action = _ACTION_ADD
                    size   = delta
                else:
                    action = _ACTION_UPDATE
                    size   = abs(delta)
                rows.append(_row(action, side, price, size, ts_ns, flags=_FLAG_LAST))

        if not rows:
            return pd.DataFrame(columns=[
                "instrument_id", "action", "side", "price", "size",
                "order_id", "flags", "sequence", "ts_event", "ts_init",
            ])

        df = pd.DataFrame(rows)
        df = df.astype({
            "action":   "uint8",
            "side":     "uint8",
            "price":    "int64",
            "size":     "uint64",
            "order_id": "uint64",
            "flags":    "uint8",
            "sequence": "uint64",
            "ts_event": "uint64",
            "ts_init":  "uint64",
        })
        return df

    def download_nautilus(
        self,
        exchange: str,
        instrument_id: str,
        slug: str,
        *,
        ticker: str | None = None,
        market: str | None = None,
        date: str | None = None,
        timeframe: str | None = None,
        md_type: str | None = None,
        price_precision: int = 4,
        size_precision: int = 2,
    ) -> pd.DataFrame:
        """
        Download orderbook data and return a NautilusTrader-compatible OrderBookDelta DataFrame.

        Convenience wrapper combining client.orderbook() and client.to_nautilus().

        Parameters
        ----------
        exchange : str
            Exchange name: "polymarket", "kalshi", "limitless", "predict-fun",
            "hyperliquid", "binance", "deribit", "bitfinex", "opinion", etc.
        instrument_id : str
            NautilusTrader instrument identifier, e.g. "TRUMP.POLYMARKET".
        slug : str
            File name within the date folder (no extension required for .gz files).
        ticker, market, date, timeframe, md_type : optional
            Passed through to client.orderbook().
        price_precision : int
            Fixed-point precision for prices. Default 4 (suitable for prediction markets
            with 0–1 prices). Use 1–2 for perpetuals.
        size_precision : int
            Fixed-point precision for sizes. Default 2.

        Returns
        -------
        pd.DataFrame
            NautilusTrader OrderBookDelta schema. See to_nautilus() for column details.
        """
        messages = self.orderbook(
            exchange=exchange,
            slug=slug,
            ticker=ticker,
            market=market,
            date=date,
            timeframe=timeframe,
            md_type=md_type,
        )
        return self.to_nautilus(
            messages,
            instrument_id=instrument_id,
            price_precision=price_precision,
            size_precision=size_precision,
        )
