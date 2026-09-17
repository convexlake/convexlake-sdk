"""
Integration + unit tests for the ConvexLake Python SDK.

Run:
    cd convexlake-sdk
    pip install -e ".[dev]"
    pytest tests/ -v

Integration tests hit the real API.  Unit tests mock the HTTP layer.
"""

import ast
import gzip
import io
import json

import pandas as pd
import pytest
import responses as resp

from convexlake import (
    AuthError,
    ConvexLake,
    ConvexLakeError,
    NotFoundError,
    PlanError,
    RateLimitError,
)
from convexlake.client import BASE_URL

# client fixture is provided by conftest.py


# ── Helpers ───────────────────────────────────────────────────────────────────

def _csv_gz(rows: list[dict]) -> bytes:
    """Build a gzip-compressed CSV bytes object from a list of dicts."""
    import csv, io as _io
    buf = _io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return gzip.compress(buf.getvalue().encode())


def _plain_csv(rows: list[dict]) -> bytes:
    import csv, io as _io
    buf = _io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode()


def _ndl_python(records: list[dict]) -> bytes:
    """Newline-delimited Python dict literals (Polymarket BBO format)."""
    return "\n".join(str(r) for r in records).encode()


def _ndl_json(records: list[dict]) -> bytes:
    """Newline-delimited JSON (Limitless format)."""
    return "\n".join(json.dumps(r) for r in records).encode()


def _concat_python(records: list[dict]) -> bytes:
    """Concatenated Python dict literals with no separator (Kalshi format)."""
    return "".join(str(r) for r in records).encode()


SAMPLE_ROWS = [
    {"timestamp": 1000, "price": 0.50, "volume": 100.0},
    {"timestamp": 2000, "price": 0.55, "volume": 200.0},
]
SAMPLE_MSGS = [
    {"type": "orderbook_snapshot", "seq": 1, "market": "test"},
    {"type": "orderbook_delta",    "seq": 2, "market": "test"},
]


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — _decompress
# ─────────────────────────────────────────────────────────────────────────────

class TestDecompress:
    @resp.activate
    def test_raw_gzip_magic_bytes_decompressed(self):
        """Raw .gz response (no Content-Encoding header) is decompressed."""
        gz = gzip.compress(b"hello,world\n1,2\n")
        resp.add(resp.GET, f"{BASE_URL}/candles", body=gz,
                 content_type="binary/octet-stream")
        c = ConvexLake(api_key="key")
        r = c._session.get(f"{BASE_URL}/candles")
        result = ConvexLake._decompress(r)
        assert result == b"hello,world\n1,2\n"

    def test_content_encoding_gzip_not_double_decompressed(self):
        """When Content-Encoding:gzip is set, requests/urllib3 already decompresses.
        _decompress must return the content as-is without a second gzip pass."""
        from unittest.mock import MagicMock
        plain = b"already,decompressed\n1,2\n"
        fake_r = MagicMock()
        fake_r.headers = {"Content-Encoding": "gzip"}
        fake_r.content = plain  # simulate what requests exposes after auto-decompression
        result = ConvexLake._decompress(fake_r)
        assert result == plain

    @resp.activate
    def test_plain_text_returned_as_is(self):
        """Non-gzip response is returned unchanged."""
        plain = b"col1,col2\nA,B\n"
        resp.add(resp.GET, f"{BASE_URL}/candles", body=plain,
                 content_type="binary/octet-stream")
        c = ConvexLake(api_key="key")
        r = c._session.get(f"{BASE_URL}/candles")
        result = ConvexLake._decompress(r)
        assert result == plain


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — _to_df
# ─────────────────────────────────────────────────────────────────────────────

class TestToDataFrame:
    def test_basic_csv_parses(self):
        raw = b"a,b,c\n1,2,3\n4,5,6\n"
        df = ConvexLake._to_df(raw)
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["a", "b", "c"]
        assert len(df) == 2

    def test_gz_csv_readable(self):
        raw = gzip.compress(b"x,y\n10,20\n30,40\n")
        decompressed = gzip.decompress(raw)
        df = ConvexLake._to_df(decompressed)
        assert len(df) == 2
        assert list(df.columns) == ["x", "y"]

    def test_numeric_columns_inferred(self):
        raw = b"price,volume\n0.50,100\n0.55,200\n"
        df = ConvexLake._to_df(raw)
        assert df["price"].dtype.kind == "f"
        assert df["volume"].dtype.kind in ("i", "f")


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — _parse_orderbook
# ─────────────────────────────────────────────────────────────────────────────

class TestParseOrderbook:
    def test_newline_delimited_python_dicts(self):
        raw = _ndl_python(SAMPLE_MSGS)
        result = ConvexLake._parse_orderbook(raw)
        assert len(result) == 2
        assert result[0]["type"] == "orderbook_snapshot"
        assert result[1]["type"] == "orderbook_delta"

    def test_newline_delimited_json(self):
        raw = _ndl_json(SAMPLE_MSGS)
        result = ConvexLake._parse_orderbook(raw, fmt="json")
        assert len(result) == 2
        assert result[0]["type"] == "orderbook_snapshot"

    def test_auto_detects_json_by_double_quotes(self):
        raw = _ndl_json(SAMPLE_MSGS)
        result = ConvexLake._parse_orderbook(raw, fmt="auto")
        assert len(result) == 2

    def test_auto_detects_python_by_single_quotes(self):
        raw = _ndl_python(SAMPLE_MSGS)
        result = ConvexLake._parse_orderbook(raw, fmt="auto")
        assert len(result) == 2

    def test_concat_python_dicts_no_separator(self):
        """Kalshi format: all dicts concatenated with no newline."""
        raw = _concat_python(SAMPLE_MSGS)
        assert b"\n" not in raw          # no newlines
        result = ConvexLake._parse_orderbook(raw, fmt="auto")
        assert len(result) == 2
        assert result[0]["type"] == "orderbook_snapshot"

    def test_auto_detects_concat_format(self):
        raw = _concat_python(SAMPLE_MSGS * 10)
        result = ConvexLake._parse_orderbook(raw, fmt="auto")
        assert len(result) == 20

    def test_invalid_lines_skipped(self):
        lines = [str(SAMPLE_MSGS[0]), "not a dict {{{", str(SAMPLE_MSGS[1])]
        raw = "\n".join(lines).encode()
        result = ConvexLake._parse_orderbook(raw, fmt="python")
        assert len(result) == 2

    def test_empty_input(self):
        assert ConvexLake._parse_orderbook(b"") == []

    def test_gzip_compressed_input(self):
        gz = gzip.compress(_ndl_python(SAMPLE_MSGS))
        decompressed = gzip.decompress(gz)
        result = ConvexLake._parse_orderbook(decompressed, fmt="python")
        assert len(result) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — error mapping
# ─────────────────────────────────────────────────────────────────────────────

class TestErrorMapping:
    @resp.activate
    def test_401_raises_auth_error(self):
        resp.add(resp.GET, f"{BASE_URL}/info",
                 json={"error": "unauthorized"}, status=401)
        with pytest.raises(AuthError):
            ConvexLake(api_key="bad").info()

    @resp.activate
    def test_403_raises_plan_error(self):
        resp.add(resp.GET, f"{BASE_URL}/candles",
                 json={"error": "forbidden"}, status=403)
        with pytest.raises(PlanError):
            ConvexLake(api_key="k").candles(
                exchange="kalshi", slug="x.csv", ticker="btc", date="2026-01-01")

    @resp.activate
    def test_404_raises_not_found(self):
        resp.add(resp.GET, f"{BASE_URL}/candles",
                 json={"message": "no data"}, status=404)
        with pytest.raises(NotFoundError):
            ConvexLake(api_key="k").candles(
                exchange="kalshi", slug="x.csv", ticker="btc", date="2026-01-01")

    @resp.activate
    def test_400_raises_convexlake_error(self):
        resp.add(resp.GET, f"{BASE_URL}/candles",
                 json={"error": "bad request"}, status=400)
        with pytest.raises(ConvexLakeError):
            ConvexLake(api_key="k").candles(
                exchange="kalshi", slug="x.csv", ticker="btc", date="2026-01-01")

    @resp.activate
    def test_429_retries_then_raises(self):
        for _ in range(3):
            resp.add(resp.GET, f"{BASE_URL}/candles",
                     json={"error": "rate limit"}, status=429,
                     headers={"Retry-After": "0"})
        with pytest.raises(RateLimitError):
            ConvexLake(api_key="k", max_retries=3).candles(
                exchange="kalshi", slug="x.csv", ticker="btc", date="2026-01-01")

    @resp.activate
    def test_429_recovers_after_retry(self):
        resp.add(resp.GET, f"{BASE_URL}/candles",
                 json={"error": "rate limit"}, status=429,
                 headers={"Retry-After": "0"})
        resp.add(resp.GET, f"{BASE_URL}/candles",
                 body=_plain_csv(SAMPLE_ROWS),
                 content_type="binary/octet-stream", status=200)
        df = ConvexLake(api_key="k", max_retries=3).candles(
            exchange="kalshi", slug="x.csv", ticker="btc", date="2026-01-01")
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — deribit orderbook guard
# ─────────────────────────────────────────────────────────────────────────────

class TestDeribitOrderbookGuard:
    @resp.activate
    def test_orderbook_deribit_raises_helpful_error(self):
        """orderbook() for deribit always raises pointing to orderbook_df()."""
        with pytest.raises(ConvexLakeError, match="orderbook_df"):
            ConvexLake(api_key="k").orderbook(
                exchange="deribit", slug="BTC-28AUG26-80000-C-book.csv",
                ticker="BTC", date="2026-08-07")


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /info
# ─────────────────────────────────────────────────────────────────────────────

class TestInfo:
    def test_returns_dict_with_tier(self, client):
        result = client.info()
        assert isinstance(result, dict)
        assert "tier" in result

    def test_lists_allowed_exchanges(self, client):
        result = client.info()
        exchanges = result.get("allowed_exchanges", [])
        assert isinstance(exchanges, list)
        assert len(exchanges) > 0

    def test_lists_top_level_folders(self, client):
        result = client.info()
        folders = result.get("folders", [])
        expected = {"candles/", "orderbook/", "trades/", "surface/"}
        assert expected.issubset(set(folders))

    def test_exchange_param_accepted(self, client):
        result = client.info(exchange="kalshi")
        assert isinstance(result, dict)

    def test_bad_key_raises_auth_error(self):
        bad = ConvexLake(api_key="do_INVALID")
        with pytest.raises(AuthError):
            bad.info()


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /candles  (predict-fun)
# ─────────────────────────────────────────────────────────────────────────────

PREDICT_CANDLE = dict(
    exchange="predict", slug="btc-updown-5m-1785385800.csv",
    ticker="crypto", date="2026-07-30",
)

class TestCandles:
    def test_predict_returns_dataframe(self, client):
        df = client.candles(**PREDICT_CANDLE)
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_predict_expected_columns(self, client):
        df = client.candles(**PREDICT_CANDLE)
        expected = {"slug", "starts_at", "ends_at", "market_variant",
                    "title", "winning_outcome"}
        assert expected.issubset(set(df.columns))

    def test_plain_csv_no_gzip_readable(self, client):
        """Predict-fun candles are plain CSV (no gzip) — _decompress must handle them."""
        df = client.candles(**PREDICT_CANDLE)
        assert not df.empty
        # Column names must be strings (CSV header was parsed correctly)
        assert all(isinstance(c, str) for c in df.columns)

    def test_not_found_raises(self, client):
        with pytest.raises(NotFoundError):
            client.candles(exchange="predict", slug="nonexistent.csv",
                           ticker="crypto", date="1990-01-01")


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /trades  (deribit options)
# ─────────────────────────────────────────────────────────────────────────────

DERIBIT_TRADE = dict(
    exchange="deribit", slug="BTC-28AUG26-80000-C-trades.csv",
    ticker="BTC", date="2026-08-07",
)

class TestTrades:
    def test_deribit_returns_dataframe(self, client):
        df = client.trades(**DERIBIT_TRADE)
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_deribit_has_expected_columns(self, client):
        df = client.trades(**DERIBIT_TRADE)
        expected = {"timestamp_ms", "trade_id", "instrument_name",
                    "direction", "amount", "price", "iv"}
        assert expected.issubset(set(df.columns))

    def test_deribit_price_column_numeric(self, client):
        df = client.trades(**DERIBIT_TRADE)
        assert df["price"].dtype.kind == "f"

    def test_deribit_iv_column_present(self, client):
        df = client.trades(**DERIBIT_TRADE)
        assert "iv" in df.columns

    def test_not_found_raises(self, client):
        with pytest.raises(NotFoundError):
            client.trades(exchange="deribit", slug="nonexistent.csv",
                          ticker="BTC", date="1990-01-01")


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /orderbook  (polymarket BBO — newline-delimited Python)
# ─────────────────────────────────────────────────────────────────────────────

POLY_ORDERBOOK = dict(
    exchange="polymarket", slug="btc-updown-15m-1776943800-bbo.gz",
    ticker="btc", timeframe="15m", date="2026-04-23",
)

class TestOrderbookPolymarket:
    def test_returns_list_of_dicts(self, client):
        msgs = client.orderbook(**POLY_ORDERBOOK)
        assert isinstance(msgs, list)
        assert len(msgs) > 0

    def test_items_are_dicts(self, client):
        msgs = client.orderbook(**POLY_ORDERBOOK)
        assert all(isinstance(m, dict) for m in msgs[:10])

    def test_bbo_fields_present(self, client):
        msgs = client.orderbook(**POLY_ORDERBOOK)
        first = msgs[0]
        assert "best_bid" in first or "best_ask" in first
        assert "timestamp" in first

    def test_gz_file_decompressed(self, client):
        """File ends in .gz but has no Content-Encoding header — magic bytes path."""
        msgs = client.orderbook(**POLY_ORDERBOOK)
        assert len(msgs) > 100  # a 15m window has many BBO events


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /orderbook  (kalshi — concat format)
# ─────────────────────────────────────────────────────────────────────────────

KALSHI_ORDERBOOK = dict(
    exchange="kalshi", slug="KXBTC15M-26MAY181245.gz",
    ticker="btc", timeframe="15m", date="2026-05-18",
)

class TestOrderbookKalshi:
    def test_returns_list_of_dicts(self, client):
        msgs = client.orderbook(**KALSHI_ORDERBOOK)
        assert isinstance(msgs, list)
        assert len(msgs) > 0

    def test_concat_format_parsed(self, client):
        """Kalshi files have no newlines — concat format must be auto-detected."""
        msgs = client.orderbook(**KALSHI_ORDERBOOK)
        assert len(msgs) > 1000  # large file with many messages

    def test_message_types(self, client):
        msgs = client.orderbook(**KALSHI_ORDERBOOK)
        types = {m.get("type") for m in msgs[:200]}
        assert "orderbook_snapshot" in types

    def test_snapshot_has_book_data(self, client):
        msgs = client.orderbook(**KALSHI_ORDERBOOK)
        snapshots = [m for m in msgs if m.get("type") == "orderbook_snapshot"]
        assert len(snapshots) > 0
        snap = snapshots[0]
        assert "msg" in snap
        assert "yes_dollars_fp" in snap["msg"] or "no_dollars_fp" in snap["msg"]


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /surface (deribit)
# ─────────────────────────────────────────────────────────────────────────────

class TestSurface:
    def test_deribit_surface_returns_dataframe(self, client):
        df = client.surface(
            exchange="deribit",
            date="2026-09-14", slug="BTC-2026-09-14",
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_deribit_surface_columns(self, client):
        df = client.surface(exchange="deribit", date="2026-09-14", slug="BTC-2026-09-14")
        expected = {"snapshot_ts", "instrument_name", "strike", "option_type",
                    "mark_iv", "mark_price"}
        assert expected.issubset(set(df.columns))

    def test_deribit_surface_mark_iv_numeric(self, client):
        df = client.surface(exchange="deribit", date="2026-09-14", slug="BTC-2026-09-14")
        assert df["mark_iv"].dtype.kind == "f"

    def test_deribit_surface_not_found_raises(self, client):
        with pytest.raises(NotFoundError):
            client.surface(exchange="deribit", date="1990-01-01", slug="BTC-1990-01-01")

    def test_orderbook_df_deribit_or_skip(self, client):
        try:
            df = client.orderbook_df(
                exchange="deribit", slug="BTC-28AUG26-80000-C-book.csv",
                ticker="BTC", date="2026-08-07",
            )
            assert isinstance(df, pd.DataFrame)
        except NotFoundError:
            pytest.skip("Deribit orderbook_df not available at tested slug — skip")


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /candles (deribit rolling — no ticker/date)
# ─────────────────────────────────────────────────────────────────────────────

DERIBIT_CANDLE = dict(exchange="deribit", timeframe="15m", slug="BTC-PERPETUAL.csv")

class TestDeribitCandles:
    def test_returns_dataframe(self, client):
        df = client.candles(**DERIBIT_CANDLE)
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_expected_columns(self, client):
        df = client.candles(**DERIBIT_CANDLE)
        expected = {"timestamp_ms", "open", "high", "low", "close", "volume"}
        assert expected.issubset(set(df.columns))

    def test_no_ticker_or_date_required(self, client):
        """Deribit rolling candles use timeframe + slug only — ticker/date must be optional."""
        df = client.candles(exchange="deribit", timeframe="15m", slug="ETH-PERPETUAL.csv")
        assert len(df) > 0


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /leaderboard
# ─────────────────────────────────────────────────────────────────────────────

class TestLeaderboard:
    def test_polymarket_returns_dataframe(self, client):
        df = client.leaderboard(exchange="polymarket", date="2026-09-14")
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_polymarket_has_expected_columns(self, client):
        df = client.leaderboard(exchange="polymarket", date="2026-09-14")
        expected = {"rank", "proxyWallet", "vol", "pnl"}
        assert expected.issubset(set(df.columns))

    def test_limitless_returns_dataframe(self, client):
        df = client.leaderboard(exchange="limitless", date="2026-09-14")
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_limitless_has_expected_columns(self, client):
        df = client.leaderboard(exchange="limitless", date="2026-09-14")
        expected = {"rank", "account", "positionSize_usd"}
        assert expected.issubset(set(df.columns))

    def test_not_found_raises(self, client):
        with pytest.raises(NotFoundError):
            client.leaderboard(exchange="polymarket", date="1990-01-01")


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — /leaderboard-trades
# ─────────────────────────────────────────────────────────────────────────────

class TestLeaderboardTrades:
    def test_list_returns_dict_with_addresses(self, client):
        result = client.leaderboard_trades(exchange="polymarket")
        assert isinstance(result, dict)
        assert "addresses" in result
        assert isinstance(result["addresses"], list)
        assert len(result["addresses"]) > 0

    def test_address_is_hex_string(self, client):
        result = client.leaderboard_trades(exchange="polymarket")
        addr = result["addresses"][0]
        assert addr.startswith("0x")

    def test_fetch_trader_returns_dataframe(self, client):
        result = client.leaderboard_trades(exchange="polymarket")
        addr = result["addresses"][0]
        df = client.leaderboard_trades(exchange="polymarket", address=addr)
        assert isinstance(df, pd.DataFrame)

    def test_limitless_list_returns_addresses(self, client):
        result = client.leaderboard_trades(exchange="limitless")
        assert isinstance(result, dict)
        assert "addresses" in result
