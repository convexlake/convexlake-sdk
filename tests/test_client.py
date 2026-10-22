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


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — _get_ts_ns
# ─────────────────────────────────────────────────────────────────────────────

class TestGetTsNs:
    def test_milliseconds_converted(self):
        msg = {"local_timestamp": 1_700_000_000_000}
        assert ConvexLake._get_ts_ns(msg) == 1_700_000_000_000 * 1_000_000

    def test_nanoseconds_passed_through(self):
        ns = 1_700_000_000_000_000_000
        msg = {"local_timestamp": ns}
        assert ConvexLake._get_ts_ns(msg) == ns

    def test_microseconds_converted(self):
        us = 1_700_000_000_000_000
        msg = {"ts": us}
        assert ConvexLake._get_ts_ns(msg) == us * 1_000

    def test_falls_back_through_field_list(self):
        msg = {"timestamp": 1_700_000_000_000}
        assert ConvexLake._get_ts_ns(msg) > 0

    def test_missing_ts_returns_zero(self):
        assert ConvexLake._get_ts_ns({}) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — to_nautilus
# ─────────────────────────────────────────────────────────────────────────────

_SNAP_MSG = {
    "type": "snapshot",
    "bids": [["0.79", 5.0], ["0.39", 12.0]],
    "asks": [["0.84", 163.625], ["0.86", 9.75]],
    "local_timestamp": 1_792_513_420_185,
}
_BBO_MSG = {
    "type": "bbo",
    "bid": 0.53, "bid_size": 100.0,
    "ask": 0.56, "ask_size":  50.0,
    "local_timestamp": 1_792_513_430_000,
}
_BEST_BBO_MSG = {
    "best_bid": 0.60, "best_ask": 0.62,
    "timestamp": 1_792_513_440_000,
}
_KALSHI_SNAP = {
    "type": "orderbook_snapshot",
    "msg": {
        "yes_dollars_fp": [[53, 10], [52, 5]],
        "no_dollars_fp":  [[47, 8],  [48, 3]],
    },
}
_KALSHI_DELTA_ADD = {
    "type": "orderbook_delta",
    "msg": {"side": "yes", "price": 54, "delta": 7},
}
_KALSHI_DELTA_DEL = {
    "type": "orderbook_delta",
    "msg": {"side": "no", "price": 48, "delta": 0},
}


class TestToNautilus:
    ISTR = "TRUMP.POLYMARKET"

    def _basic(self, msgs=None):
        return ConvexLake.to_nautilus(
            msgs or [_SNAP_MSG], self.ISTR, price_precision=4, size_precision=2,
        )

    # ── Output shape ──────────────────────────────────────────────────────────

    def test_returns_dataframe(self):
        df = self._basic()
        assert isinstance(df, pd.DataFrame)

    def test_expected_columns(self):
        df = self._basic()
        expected = {"instrument_id", "action", "side", "price", "size",
                    "order_id", "flags", "sequence", "ts_event", "ts_init"}
        assert expected == set(df.columns)

    def test_empty_input_returns_empty_df(self):
        df = ConvexLake.to_nautilus([], self.ISTR)
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 0

    def test_dtypes(self):
        df = self._basic()
        assert df["action"].dtype   == "uint8"
        assert df["side"].dtype     == "uint8"
        assert df["price"].dtype    == "int64"
        assert df["size"].dtype     == "uint64"
        assert df["order_id"].dtype == "uint64"
        assert df["flags"].dtype    == "uint8"
        assert df["sequence"].dtype == "uint64"
        assert df["ts_event"].dtype == "uint64"
        assert df["ts_init"].dtype  == "uint64"

    # ── Snapshot expansion ────────────────────────────────────────────────────

    def test_snapshot_starts_with_clear(self):
        df = self._basic()
        assert df.iloc[0]["action"] == 4  # CLEAR

    def test_snapshot_clear_side_is_none(self):
        df = self._basic()
        assert df.iloc[0]["side"] == 0

    def test_snapshot_bids_are_buy(self):
        df = self._basic()
        adds = df[df["action"] == 1]
        bid_rows = adds[adds["side"] == 1]
        assert len(bid_rows) == 2

    def test_snapshot_asks_are_sell(self):
        df = self._basic()
        adds = df[df["action"] == 1]
        ask_rows = adds[adds["side"] == 2]
        assert len(ask_rows) == 2

    def test_snapshot_row_count(self):
        df = self._basic()
        # 1 CLEAR + 2 bids + 2 asks = 5
        assert len(df) == 5

    def test_snapshot_last_row_has_flag_f_last(self):
        df = self._basic()
        assert df.iloc[-1]["flags"] == 1

    def test_snapshot_non_last_rows_have_flag_zero(self):
        df = self._basic()
        assert (df.iloc[:-1]["flags"] == 0).all()

    def test_price_fixed_point_encoding(self):
        df = self._basic()
        best_bid_row = df[(df["action"] == 1) & (df["side"] == 1)].iloc[0]
        # 0.79 * 10^4 = 7900
        assert best_bid_row["price"] == 7900

    def test_size_fixed_point_encoding(self):
        df = self._basic()
        best_bid_row = df[(df["action"] == 1) & (df["side"] == 1)].iloc[0]
        # 5.0 * 10^2 = 500
        assert best_bid_row["size"] == 500

    def test_timestamp_nanoseconds(self):
        df = self._basic()
        expected_ns = 1_792_513_420_185 * 1_000_000
        assert (df["ts_event"] == expected_ns).all()

    def test_ts_event_equals_ts_init(self):
        df = self._basic()
        assert (df["ts_event"] == df["ts_init"]).all()

    def test_instrument_id_set(self):
        df = self._basic()
        assert (df["instrument_id"] == self.ISTR).all()

    def test_order_id_is_zero(self):
        df = self._basic()
        assert (df["order_id"] == 0).all()

    def test_sequence_monotonic(self):
        df = self._basic()
        assert list(df["sequence"]) == list(range(1, len(df) + 1))

    # ── Multiple snapshots → sequence continues ───────────────────────────────

    def test_two_snapshots_sequence_is_continuous(self):
        df = ConvexLake.to_nautilus([_SNAP_MSG, _SNAP_MSG], self.ISTR)
        assert df["sequence"].is_monotonic_increasing
        assert df.iloc[-1]["sequence"] == 10  # 5 rows × 2

    def test_each_snapshot_ends_with_flag(self):
        df = ConvexLake.to_nautilus([_SNAP_MSG, _SNAP_MSG], self.ISTR)
        flagged = df[df["flags"] == 1]
        assert len(flagged) == 2  # one per snapshot

    # ── BBO messages ─────────────────────────────────────────────────────────

    def test_bbo_produces_three_rows(self):
        df = ConvexLake.to_nautilus([_BBO_MSG], self.ISTR, price_precision=4)
        assert len(df) == 3  # CLEAR + BUY + SELL

    def test_bbo_last_row_f_last(self):
        df = ConvexLake.to_nautilus([_BBO_MSG], self.ISTR)
        assert df.iloc[-1]["flags"] == 1

    def test_bbo_best_bid_ask_format(self):
        df = ConvexLake.to_nautilus([_BEST_BBO_MSG], self.ISTR)
        assert len(df) == 3

    # ── Kalshi snapshot ───────────────────────────────────────────────────────

    def test_kalshi_snapshot_row_count(self):
        df = ConvexLake.to_nautilus([_KALSHI_SNAP], self.ISTR, price_precision=4)
        # 1 CLEAR + 2 yes (bids) + 2 no (asks) = 5
        assert len(df) == 5

    def test_kalshi_snapshot_price_in_cents_normalized(self):
        df = ConvexLake.to_nautilus([_KALSHI_SNAP], self.ISTR, price_precision=4)
        bids = df[(df["action"] == 1) & (df["side"] == 1)]
        # 53 cents / 100 = 0.53 → 0.53 * 10^4 = 5300
        assert bids.iloc[0]["price"] == 5300

    def test_kalshi_snapshot_ends_with_f_last(self):
        df = ConvexLake.to_nautilus([_KALSHI_SNAP], self.ISTR)
        assert df.iloc[-1]["flags"] == 1

    # ── Kalshi delta ─────────────────────────────────────────────────────────

    def test_kalshi_delta_add(self):
        df = ConvexLake.to_nautilus([_KALSHI_DELTA_ADD], self.ISTR, price_precision=4)
        assert len(df) == 1
        assert df.iloc[0]["action"] == 1   # ADD
        assert df.iloc[0]["side"]   == 1   # BUY (yes)
        assert df.iloc[0]["size"]   == 700 # 7 * 10^2
        assert df.iloc[0]["flags"]  == 1   # F_LAST

    def test_kalshi_delta_delete(self):
        df = ConvexLake.to_nautilus([_KALSHI_DELTA_DEL], self.ISTR, price_precision=4)
        assert df.iloc[0]["action"] == 3   # DELETE
        assert df.iloc[0]["side"]   == 2   # SELL (no)

    # ── Custom precision ─────────────────────────────────────────────────────

    def test_price_precision_applied(self):
        msg = {"bids": [["65000.5", 1.0]], "asks": [], "local_timestamp": 0}
        df = ConvexLake.to_nautilus([msg], "BTC-PERP.BINANCE", price_precision=1)
        # 65000.5 * 10 = 650005
        bids = df[df["side"] == 1]
        assert bids.iloc[0]["price"] == 650005


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — download_nautilus (mocked HTTP)
# ─────────────────────────────────────────────────────────────────────────────

class TestDownloadNautilus:
    @resp.activate
    def test_returns_nautilus_dataframe(self):
        msgs = [_SNAP_MSG]
        raw = gzip.compress(_ndl_json(msgs))
        resp.add(resp.GET, f"{BASE_URL}/orderbook", body=raw,
                 content_type="application/gzip")
        df = ConvexLake(api_key="k").download_nautilus(
            exchange="polymarket",
            instrument_id="TRUMP.POLYMARKET",
            slug="some-market-book.gz",
            ticker="btc", date="2026-04-23",
        )
        assert isinstance(df, pd.DataFrame)
        assert "action" in df.columns
        assert len(df) == 5  # 1 CLEAR + 2 bids + 2 asks

    @resp.activate
    def test_price_precision_forwarded(self):
        msgs = [{"bids": [["0.79", 5.0]], "asks": [], "local_timestamp": 0}]
        raw = gzip.compress(_ndl_json(msgs))
        resp.add(resp.GET, f"{BASE_URL}/orderbook", body=raw,
                 content_type="application/gzip")
        df = ConvexLake(api_key="k").download_nautilus(
            exchange="polymarket",
            instrument_id="TRUMP.POLYMARKET",
            slug="some-market-book.gz",
            price_precision=2,
        )
        bids = df[df["side"] == 1]
        assert bids.iloc[0]["price"] == 79  # 0.79 * 10^2


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — orderbook new params (market, md_type)
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderbookNewParams:
    @resp.activate
    def test_market_param_forwarded(self):
        raw = gzip.compress(_ndl_json([_SNAP_MSG]))
        resp.add(resp.GET, f"{BASE_URL}/orderbook", body=raw,
                 content_type="application/gzip")
        ConvexLake(api_key="k").orderbook(
            exchange="opinion", slug="some-book.gz",
            market="BTCUSDT", date="2026-10-01",
        )
        call = resp.calls[0]
        assert "market=BTCUSDT" in call.request.url

    @resp.activate
    def test_md_type_param_forwarded(self):
        raw = gzip.compress(_ndl_json([_SNAP_MSG]))
        resp.add(resp.GET, f"{BASE_URL}/orderbook", body=raw,
                 content_type="application/gzip")
        ConvexLake(api_key="k").orderbook(
            exchange="polymarket", slug="btc-bbo.gz",
            ticker="btc", date="2026-10-01", md_type="bbo",
        )
        call = resp.calls[0]
        assert "md_type=bbo" in call.request.url

    @resp.activate
    def test_none_params_not_forwarded(self):
        raw = gzip.compress(_ndl_json([_SNAP_MSG]))
        resp.add(resp.GET, f"{BASE_URL}/orderbook", body=raw,
                 content_type="application/gzip")
        ConvexLake(api_key="k").orderbook(
            exchange="polymarket", slug="btc-bbo.gz",
        )
        call = resp.calls[0]
        assert "market=" not in call.request.url
        assert "md_type=" not in call.request.url


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — orderbook_weather
# ─────────────────────────────────────────────────────────────────────────────

class TestOrderbookWeather:
    @resp.activate
    def test_returns_raw_bytes(self):
        raw = gzip.compress(b"some,weather,data\n1,2,3\n")
        resp.add(resp.GET, f"{BASE_URL}/orderbook-weather", body=raw,
                 content_type="application/gzip")
        result = ConvexLake(api_key="k").orderbook_weather(
            exchange="weather", market="NYC",
            date="2026-10-01", slug="NYC-2026-10-01.gz",
        )
        assert isinstance(result, bytes)

    @resp.activate
    def test_presign_returns_dict(self):
        resp.add(resp.GET, f"{BASE_URL}/orderbook-weather",
                 json={"url": "https://s3.example.com/presigned?token=abc"},
                 content_type="application/json")
        result = ConvexLake(api_key="k").orderbook_weather(
            exchange="weather", market="NYC",
            date="2026-10-01", slug="NYC-2026-10-01.gz",
            presign=True,
        )
        assert isinstance(result, dict)
        assert "url" in result

    @resp.activate
    def test_presign_param_forwarded(self):
        resp.add(resp.GET, f"{BASE_URL}/orderbook-weather",
                 json={"url": "https://example.com/file"},
                 content_type="application/json")
        ConvexLake(api_key="k").orderbook_weather(
            exchange="weather", market="NYC",
            date="2026-10-01", slug="NYC-2026-10-01.gz",
            presign=True, expires=600,
        )
        call = resp.calls[0]
        assert "presign=1" in call.request.url
        assert "expires=600" in call.request.url


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests — info market param
# ─────────────────────────────────────────────────────────────────────────────

class TestInfoMarketParam:
    @resp.activate
    def test_market_param_forwarded(self):
        resp.add(resp.GET, f"{BASE_URL}/info",
                 json={"tier": "free", "folders": []})
        ConvexLake(api_key="k").info(exchange="opinion", market="BTCUSDT")
        call = resp.calls[0]
        assert "market=BTCUSDT" in call.request.url

    @resp.activate
    def test_market_none_not_forwarded(self):
        resp.add(resp.GET, f"{BASE_URL}/info",
                 json={"tier": "free", "folders": []})
        ConvexLake(api_key="k").info(exchange="polymarket")
        call = resp.calls[0]
        assert "market=" not in call.request.url


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — to_nautilus on real Kalshi data
# ─────────────────────────────────────────────────────────────────────────────

class TestToNautilusKalshiIntegration:
    def test_kalshi_orderbook_converts_to_nautilus(self, client):
        msgs = client.orderbook(**KALSHI_ORDERBOOK)
        df = ConvexLake.to_nautilus(msgs, "KXBTC15M.KALSHI", price_precision=4)
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0
        assert set(df["action"]).issubset({1, 2, 3, 4})  # valid NautilusTrader actions

    def test_kalshi_nautilus_dtypes(self, client):
        msgs = client.orderbook(**KALSHI_ORDERBOOK)
        df = ConvexLake.to_nautilus(msgs, "KXBTC15M.KALSHI", price_precision=4)
        assert df["price"].dtype  == "int64"
        assert df["ts_event"].dtype == "uint64"

    def test_kalshi_nautilus_prices_in_range(self, client):
        msgs = client.orderbook(**KALSHI_ORDERBOOK)
        df = ConvexLake.to_nautilus(msgs, "KXBTC15M.KALSHI", price_precision=4)
        adds = df[df["action"] == 1]
        # Prices with precision=4 for 0-1 range: should be 0–10000
        assert (adds["price"] >= 0).all()
        assert (adds["price"] <= 10000).all()


# ─────────────────────────────────────────────────────────────────────────────
# Integration tests — to_nautilus on real Polymarket BBO data
# ─────────────────────────────────────────────────────────────────────────────

class TestToNautilusPolymarketIntegration:
    def test_polymarket_bbo_converts(self, client):
        msgs = client.orderbook(**POLY_ORDERBOOK)
        df = ConvexLake.to_nautilus(msgs, "BTC-UPDOWN-15M.POLYMARKET")
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0

    def test_polymarket_bbo_actions_valid(self, client):
        msgs = client.orderbook(**POLY_ORDERBOOK)
        df = ConvexLake.to_nautilus(msgs, "BTC-UPDOWN-15M.POLYMARKET")
        assert set(df["action"]).issubset({1, 2, 3, 4})

    def test_download_nautilus_integration(self, client):
        df = client.download_nautilus(
            instrument_id="BTC-UPDOWN-15M.POLYMARKET",
            **POLY_ORDERBOOK,
        )
        assert isinstance(df, pd.DataFrame)
        assert len(df) > 0
        assert "ts_event" in df.columns
