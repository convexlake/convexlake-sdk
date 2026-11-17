"""
LangChain tools for the ConvexLake SDK.

Install:
    pip install convexlake[langchain]

Usage:
    from convexlake.langchain import get_tools
    tools = get_tools(api_key="do_...")
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool

import requests

from convexlake import ConvexLake
from convexlake.exceptions import (
    ConvexLakeError,
    MultipleChoicesError,
    NotFoundError,
    PlanError,
)


def get_tools(
    client: ConvexLake | None = None,
    *,
    api_key: str | None = None,
    max_rows: int = 20,
) -> list[BaseTool]:
    """
    Return a list of LangChain tools that wrap the ConvexLake SDK.

    Parameters
    ----------
    client : ConvexLake, optional
        An already-constructed client. If None, one is built from api_key or
        the CONVEXLAKE_API_KEY environment variable.
    api_key : str, optional
        API key override. Ignored when client is provided.
    max_rows : int
        Maximum rows to include in DataFrame summaries returned to the model.

    Returns
    -------
    list[BaseTool]
    """
    try:
        from langchain_core.tools import tool
    except ImportError:
        raise ImportError(
            "LangChain support requires langchain-core. "
            "Install with: pip install convexlake[langchain]"
        )

    if client is None:
        key = api_key or os.environ.get("CONVEXLAKE_API_KEY")
        if not key:
            raise ValueError(
                "Provide api_key= or set the CONVEXLAKE_API_KEY environment variable."
            )
        client = ConvexLake(api_key=key)

    # ── shared helper ────────────────────────────────────────────────────────

    def _summarise(df, _max_rows: int = max_rows) -> tuple[str, Any]:
        """
        Return (summary_str, full_df).

        summary_str is returned to the model (row count, columns, first rows,
        numeric describe). full_df is the tool artifact, available to calling code.
        """
        import io as _io
        rows_shown = min(_max_rows, len(df))
        lines = [
            f"rows: {len(df)}",
            f"columns: {list(df.columns)}",
            "",
            df.head(rows_shown).to_string(index=False),
        ]
        num_df = df.select_dtypes(include="number")
        if not num_df.empty:
            buf = _io.StringIO()
            num_df.describe().to_string(buf)
            lines += ["", "describe:", buf.getvalue()]
        return "\n".join(lines), df

    # ── tools ────────────────────────────────────────────────────────────────

    @tool
    def convexlake_info(
        exchange: str | None = None,
        ticker: str | None = None,
        date: str | None = None,
    ) -> dict:
        """
        Browse the ConvexLake data catalog.

        Call progressively to drill down:
          - convexlake_info()                        → list of exchanges
          - convexlake_info(exchange="kalshi")        → tickers and date ranges
          - convexlake_info(exchange="kalshi", ticker="btc")  → available dates
          - convexlake_info(exchange="kalshi", ticker="btc", date="2026-10-01")
                                                     → file slugs for that day

        Returns a dict. Slugs returned here are required by other tools such as
        convexlake_trades and convexlake_candles.

        Supported exchanges: polymarket, kalshi, limitless, predict, deribit,
        binance, hyperliquid, okx, bybit, bitfinex.
        """
        try:
            return client.catalog(exchange=exchange, ticker=ticker, date=date)
        except ConvexLakeError as e:
            return {"error": str(e)}

    @tool(response_format="content_and_artifact")
    def convexlake_trades(
        exchange: Literal["polymarket", "kalshi", "limitless", "deribit", "binance", "okx", "bybit"],
        ticker: str,
        date: str,
        slug: str | None = None,
    ) -> tuple[str, Any]:
        """
        Download trade history for a prediction market or options instrument.

        Returns a text summary (row count, columns, first rows, numeric describe)
        as content, and the full pd.DataFrame as the tool artifact.

        Parameters
        ----------
        exchange : one of polymarket, kalshi, limitless, deribit, binance, okx, bybit
        ticker   : category or asset, e.g. "crypto", "btc", "BTC"
        date     : settlement/expiry date in YYYY-MM-DD format
        slug     : file slug — required when multiple files exist for the date.
                   Call convexlake_info to find the correct slug.
        """
        try:
            df = client.pm.trades(exchange, ticker, date, slug=slug)
            return _summarise(df)
        except MultipleChoicesError as e:
            return f"Multiple files match — pass slug= one of: {e.slugs}", None
        except NotFoundError:
            return f"No trades found for {exchange}/{ticker}/{date}. Call convexlake_info to verify the slug.", None
        except PlanError:
            return "This exchange is not in your subscription tier.", None
        except (ConvexLakeError, requests.RequestException) as e:
            return f"Error fetching trades: {e}", None

    @tool(response_format="content_and_artifact")
    def convexlake_candles(
        exchange: Literal["polymarket", "kalshi", "predict", "deribit", "hyperliquid"],
        ticker: str,
        date: str | None = None,
        timeframe: str | None = None,
        slug: str | None = None,
    ) -> tuple[str, Any]:
        """
        Download OHLCV candle data for a prediction market or perpetual instrument.

        Returns a text summary as content and the full pd.DataFrame as the artifact.

        For prediction markets (polymarket, kalshi, predict):
          - date is the market settlement/expiry date (YYYY-MM-DD)
          - slug is usually required; call convexlake_info to get it

        For crypto perpetuals (deribit, hyperliquid):
          - pass timeframe (e.g. "1h", "4h", "1d") and slug (instrument name)
          - date is not used

        Columns (crypto): timestamp_ms, open, high, low, close, volume, cost
        Columns (kalshi): timestamp, yes_bid, yes_ask, price, volume
        """
        try:
            if exchange in ("deribit", "hyperliquid") and timeframe and slug:
                df = client.cx.candles(exchange, slug, timeframe)
            elif date:
                df = client.pm.candles(exchange, ticker, date, slug=slug)
            else:
                return "Provide date (prediction markets) or timeframe+slug (crypto perpetuals).", None
            return _summarise(df)
        except MultipleChoicesError as e:
            return f"Multiple files match — pass slug= one of: {e.slugs}", None
        except NotFoundError:
            return (
                f"No candles found. Call convexlake_info(exchange='{exchange}', "
                f"ticker='{ticker}') to find valid dates and slugs."
            ), None
        except PlanError:
            return "This exchange is not in your subscription tier.", None
        except (ConvexLakeError, requests.RequestException) as e:
            return f"Error fetching candles: {e}", None

    @tool(response_format="content_and_artifact")
    def convexlake_surface(
        exchange: Literal["deribit", "binance", "okx", "bybit"],
        ticker: Literal["BTC", "ETH"],
        date: str,
    ) -> tuple[str, Any]:
        """
        Download the implied volatility surface snapshot for an options exchange.

        Returns a text summary as content and the full pd.DataFrame as the artifact.

        Parameters
        ----------
        exchange : deribit, binance, okx, or bybit
        ticker   : BTC or ETH
        date     : YYYY-MM-DD

        Columns: snapshot_ts, instrument_name, expiry, strike, option_type,
                 mark_iv, bid_price, ask_price, open_interest, ...
        """
        try:
            df = client.cx.surface(exchange, ticker, date)
            return _summarise(df)
        except MultipleChoicesError as e:
            return f"Multiple files match — pass slug= one of: {e.slugs}", None
        except NotFoundError:
            return f"No surface data for {exchange}/{ticker}/{date}.", None
        except PlanError:
            return "This exchange is not in your subscription tier.", None
        except (ConvexLakeError, requests.RequestException) as e:
            return f"Error fetching surface: {e}", None

    @tool(response_format="content_and_artifact")
    def convexlake_leaderboard(
        exchange: Literal["polymarket", "limitless", "predict"],
        date: str,
    ) -> tuple[str, Any]:
        """
        Download the daily leaderboard snapshot for a prediction market exchange.

        Returns a text summary as content and the full pd.DataFrame as the artifact.

        Parameters
        ----------
        exchange : polymarket, limitless, or predict
        date     : YYYY-MM-DD — must be a date with collected data.
                   Call convexlake_info(exchange=exchange) to find the date range.

        Columns (polymarket): fetched_at, time_period, rank, proxyWallet, userName, vol, pnl
        Columns (limitless):  fetched_at, metric, rank, account, username,
                              positionSize_usd, unrealizedPnl_usd
        """
        try:
            df = client.pm.leaderboard(exchange, date)
            return _summarise(df)
        except MultipleChoicesError as e:
            return f"Multiple files match — pass slug= one of: {e.slugs}", None
        except NotFoundError:
            return (
                f"No leaderboard for {exchange}/{date}. "
                f"Call convexlake_info(exchange='{exchange}') to find the available date range."
            ), None
        except PlanError:
            return "This endpoint is not in your subscription tier.", None
        except (ConvexLakeError, requests.RequestException) as e:
            return f"Error fetching leaderboard: {e}", None

    @tool(response_format="content_and_artifact")
    def convexlake_leaderboard_trades(
        exchange: Literal["polymarket", "limitless", "predict"],
        address: str | None = None,
        max_addresses: int = 20,
    ) -> tuple[str, Any]:
        """
        List trader addresses or fetch one trader's fill history.

        Step 1 — no address: returns the first max_addresses trader addresses
          with the total count.

        Step 2 — with address: returns a summary of that trader's fill history.
          Columns (polymarket): proxyWallet, side, asset, conditionId, size, price,
                                timestamp, title, slug, outcome, transactionHash

        Parameters
        ----------
        exchange : polymarket, limitless, or predict
        address  : trader wallet address (0x...). If None, returns the address list.
        """
        try:
            if address is None:
                addresses = client.pm.traders(exchange)
                shown = addresses[:max_addresses]
                content = (
                    f"total traders: {len(addresses)}\n"
                    f"first {len(shown)}:\n" + "\n".join(shown)
                )
                return content, addresses
            df = client.pm.trader_fills(exchange, address)
            return _summarise(df)
        except MultipleChoicesError as e:
            return f"Multiple files match — pass slug= one of: {e.slugs}", None
        except NotFoundError:
            return f"No fill history for address {address} on {exchange}.", None
        except PlanError:
            return "This endpoint is not in your subscription tier.", None
        except (ConvexLakeError, requests.RequestException) as e:
            return f"Error fetching trader data: {e}", None

    @tool
    def convexlake_orderbook_summary(
        exchange: Literal["polymarket", "kalshi", "limitless", "predict", "deribit",
                          "binance", "hyperliquid", "okx", "bybit", "bitfinex"],
        ticker: str,
        timeframe: str,
        date: str,
        slug: str | None = None,
        md_type: str | None = None,
    ) -> str:
        """
        Return a summary of an orderbook file: message count, message types seen,
        and first/last timestamps. Never returns raw messages or price levels.

        Parameters
        ----------
        exchange  : any supported exchange
        ticker    : e.g. "btc", "BTC-PERPETUAL", "BTCPERP"
        timeframe : e.g. "15m", "1h"
        date      : YYYY-MM-DD
        slug      : file slug — required when multiple files exist. Call
                    convexlake_info to find it.
        md_type   : optional "bbo" to request best-bid/ask-only files.

        Supported exchanges (PM): polymarket, kalshi, limitless, predict.
        Supported exchanges (CX): deribit, binance, hyperliquid, okx, bybit, bitfinex.
        """
        PM_EXCHANGES = {"polymarket", "kalshi", "limitless", "predict"}
        try:
            if exchange in PM_EXCHANGES:
                msgs = client.pm.orderbook(
                    exchange, ticker, timeframe, date,
                    slug=slug, md_type=md_type,
                )
            else:
                result = client.cx.orderbook(exchange, ticker, timeframe, date, slug=slug)
                if not isinstance(result, list):
                    # Deribit options returns a DataFrame
                    df = result
                    return (
                        f"Orderbook (CSV format, {exchange}): "
                        f"{len(df)} rows, columns: {list(df.columns)}"
                    )
                msgs = result

            if not msgs:
                return "Orderbook is empty."

            types: dict[str, int] = {}
            for m in msgs:
                t = m.get("type") or m.get("event_type", "unknown")
                types[t] = types.get(t, 0) + 1

            def _ts(m: dict) -> int | None:
                for f in ("local_timestamp", "timestamp", "ts", "time", "created_at"):
                    v = m.get(f)
                    if v is not None:
                        return int(v)
                return None

            timestamps = [t for m in msgs if (t := _ts(m)) is not None]
            ts_summary = ""
            if timestamps:
                ts_summary = f"\nfirst_ts: {min(timestamps)}, last_ts: {max(timestamps)}"

            def _best_bbo(m: dict) -> tuple[float | None, float | None]:
                bids = m.get("bids") or []
                asks = m.get("asks") or []
                best_bid = max((float(b[0]) for b in bids if b), default=None)
                best_ask = min((float(a[0]) for a in asks if a), default=None)
                return best_bid, best_ask

            SNAP_TYPES = {"orderbook_snapshot", "snapshot", "book_snapshot"}
            snapshots = [m for m in msgs if (m.get("type") or m.get("event_type", "")) in SNAP_TYPES]
            bbo_summary = ""
            if snapshots:
                fb, fa = _best_bbo(snapshots[0])
                lb, la = _best_bbo(snapshots[-1])
                if fb is not None or fa is not None:
                    bbo_summary = (
                        f"\nfirst_best_bid: {fb}, first_best_ask: {fa}"
                        f"\nlast_best_bid: {lb}, last_best_ask: {la}"
                    )

            return (
                f"messages: {len(msgs)}"
                f"\ntypes: {types}"
                f"{ts_summary}"
                f"{bbo_summary}"
            )
        except MultipleChoicesError as e:
            return f"Multiple files match — pass slug= one of: {e.slugs}"
        except NotFoundError:
            return (
                f"No orderbook found for {exchange}/{ticker}/{date}. "
                "Call convexlake_info to find valid slugs."
            )
        except PlanError:
            return "This exchange is not in your subscription tier."
        except (ConvexLakeError, requests.RequestException) as e:
            return f"Error fetching orderbook: {e}"

    return [
        convexlake_info,
        convexlake_trades,
        convexlake_candles,
        convexlake_surface,
        convexlake_leaderboard,
        convexlake_leaderboard_trades,
        convexlake_orderbook_summary,
    ]
