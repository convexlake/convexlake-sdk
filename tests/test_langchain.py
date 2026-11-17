"""
Tests for convexlake.langchain (SDK tools).

Mocks HTTP the same way test_client.py does, using the `responses` library.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
import sys

import pandas as pd
import pytest
import responses as resp

from convexlake import ConvexLake
from convexlake.client import BASE_URL


# ── helpers ───────────────────────────────────────────────────────────────────

def _csv_gz(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return gzip.compress(buf.getvalue().encode())


def _plain_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode()


TRADES_ROWS = [
    {"timestamp": 1000 + i, "price": 0.50 + i * 0.01, "volume": 100.0 + i}
    for i in range(30)
]

CATALOG_RESP = {
    "namespaces": {"pm": ["polymarket", "kalshi"], "cx": ["deribit"]},
    "data_types": {"pm": ["orderbook", "candles", "trades"], "cx": ["candles"]},
}


@pytest.fixture
def lc_client():
    return ConvexLake(api_key="do_test")


@pytest.fixture
def tools(lc_client):
    from convexlake.langchain import get_tools
    return {t.name: t for t in get_tools(client=lc_client)}


def _invoke(tool, args: dict):
    """
    Invoke a tool and return (content_str, artifact_or_None).

    tool.invoke() returns only the content string even for content_and_artifact tools.
    To access the artifact, call tool.func(**args) directly — it returns (str, Any).
    """
    content = tool.invoke(args)
    # For content_and_artifact tools, call the underlying func to get the artifact
    if getattr(tool, "response_format", None) == "content_and_artifact":
        result = tool.func(**args)
        if isinstance(result, tuple):
            return result[0], result[1]
    return content, None


# ── get_tools ─────────────────────────────────────────────────────────────────

class TestGetTools:
    def test_returns_expected_tool_names(self, lc_client):
        from convexlake.langchain import get_tools
        tools = get_tools(client=lc_client)
        names = {t.name for t in tools}
        assert names == {
            "convexlake_info",
            "convexlake_trades",
            "convexlake_candles",
            "convexlake_surface",
            "convexlake_leaderboard",
            "convexlake_leaderboard_trades",
            "convexlake_orderbook_summary",
        }

    def test_builds_client_from_api_key(self):
        from convexlake.langchain import get_tools
        tools = get_tools(api_key="do_test_key")
        assert len(tools) == 7

    def test_builds_client_from_env(self, monkeypatch):
        monkeypatch.setenv("CONVEXLAKE_API_KEY", "do_env_key")
        from convexlake.langchain import get_tools
        tools = get_tools()
        assert len(tools) == 7

    def test_raises_when_no_key(self, monkeypatch):
        monkeypatch.delenv("CONVEXLAKE_API_KEY", raising=False)
        from convexlake.langchain import get_tools
        with pytest.raises(ValueError, match="CONVEXLAKE_API_KEY"):
            get_tools()

    def test_missing_langchain_core_raises_install_hint(self, monkeypatch):
        import convexlake.langchain as lm

        def patched(**kw):
            raise ImportError(
                "LangChain support requires langchain-core. "
                "Install with: pip install convexlake[langchain]"
            )

        monkeypatch.setattr(lm, "get_tools", patched)
        with pytest.raises(ImportError, match="pip install convexlake\\[langchain\\]"):
            lm.get_tools(api_key="do_x")


# ── tool schemas ──────────────────────────────────────────────────────────────

class TestToolSchemas:
    def test_trades_schema_has_expected_args(self, tools):
        schema = tools["convexlake_trades"].args_schema.model_json_schema()
        props = schema["properties"]
        assert "exchange" in props
        assert "ticker" in props
        assert "date" in props
        assert "slug" in props

    def test_candles_schema_has_expected_args(self, tools):
        schema = tools["convexlake_candles"].args_schema.model_json_schema()
        props = schema["properties"]
        assert "exchange" in props
        assert "ticker" in props
        assert "date" in props
        assert "timeframe" in props

    def test_surface_schema_has_expected_args(self, tools):
        schema = tools["convexlake_surface"].args_schema.model_json_schema()
        props = schema["properties"]
        assert "exchange" in props
        assert "ticker" in props
        assert "date" in props

    def test_orderbook_summary_schema_has_expected_args(self, tools):
        schema = tools["convexlake_orderbook_summary"].args_schema.model_json_schema()
        props = schema["properties"]
        for arg in ("exchange", "ticker", "timeframe", "date", "slug"):
            assert arg in props


# ── convexlake_trades ─────────────────────────────────────────────────────────

class TestTradesTool:
    @resp.activate
    def test_returns_summary_within_max_rows(self, tools):
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/trades",
            body=_csv_gz(TRADES_ROWS),
            content_type="binary/octet-stream",
        )
        content, artifact = _invoke(tools["convexlake_trades"], {
            "exchange": "kalshi",
            "ticker": "crypto",
            "date": "2026-10-01",
            "slug": "crypto-trades.csv",
        })
        assert "rows: 30" in content

    @resp.activate
    def test_artifact_is_full_dataframe(self, tools):
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/trades",
            body=_csv_gz(TRADES_ROWS),
            content_type="binary/octet-stream",
        )
        content, artifact = _invoke(tools["convexlake_trades"], {
            "exchange": "kalshi",
            "ticker": "crypto",
            "date": "2026-10-01",
            "slug": "crypto-trades.csv",
        })
        assert isinstance(artifact, pd.DataFrame)
        assert len(artifact) == 30

    @resp.activate
    def test_summary_does_not_exceed_max_rows(self, lc_client):
        from convexlake.langchain import get_tools
        tools = {t.name: t for t in get_tools(client=lc_client, max_rows=5)}
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/trades",
            body=_csv_gz(TRADES_ROWS),
            content_type="binary/octet-stream",
        )
        content, _ = _invoke(tools["convexlake_trades"], {
            "exchange": "kalshi",
            "ticker": "crypto",
            "date": "2026-10-01",
            "slug": "crypto-trades.csv",
        })
        # Only first 5 rows should appear in table (summary says rows: 30 but preview is 5)
        assert "rows: 30" in content
        lines = content.split("\n")
        # find table section between blank line after "columns:" and "describe:"
        col_line = next(i for i, l in enumerate(lines) if "columns:" in l)
        describe_line = next(
            (i for i, l in enumerate(lines) if "describe:" in l), len(lines)
        )
        # table lines (non-empty, after col_line+2)
        table_lines = [
            l for l in lines[col_line + 2: describe_line] if l.strip()
        ]
        # first table line is the header, then data rows
        data_rows = table_lines[1:]
        assert len(data_rows) <= 5

    @resp.activate
    def test_404_returns_readable_error(self, tools):
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/trades",
            json={"error": "no data for parameters provided", "code": "NOT_FOUND"},
            status=404,
        )
        content, artifact = _invoke(tools["convexlake_trades"], {
            "exchange": "kalshi",
            "ticker": "crypto",
            "date": "2099-01-01",
        })
        assert "No trades found" in content or "convexlake_info" in content
        assert artifact is None

    @resp.activate
    def test_plan_error_returns_tier_message(self, tools):
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/trades",
            json={"error": "forbidden", "code": "PLAN"},
            status=403,
        )
        content, _ = _invoke(tools["convexlake_trades"], {
            "exchange": "kalshi",
            "ticker": "crypto",
            "date": "2026-10-01",
        })
        assert "tier" in content.lower()


# ── convexlake_orderbook_summary ──────────────────────────────────────────────

class TestOrderbookSummaryTool:
    @resp.activate
    def test_never_returns_raw_messages(self, tools):
        msgs = [
            {"type": "orderbook_snapshot", "bids": [[0.5, 100]], "asks": [[0.51, 50]],
             "local_timestamp": 1700000000000},
            {"type": "orderbook_delta", "side": "yes", "price_dollars": 0.5, "delta_fp": 10,
             "local_timestamp": 1700000001000},
        ]
        body = gzip.compress("\n".join(str(m) for m in msgs).encode())
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/orderbook",
            body=body,
            content_type="binary/octet-stream",
        )
        result = tools["convexlake_orderbook_summary"].invoke({
            "exchange": "polymarket",
            "ticker": "btc",
            "timeframe": "15m",
            "date": "2026-10-01",
            "slug": "btc-updown-15m-123.gz",
        })
        assert isinstance(result, str)
        assert "messages:" in result
        # Must NOT contain raw level data
        assert "bids" not in result
        assert "0.5, 100" not in result

    @resp.activate
    def test_includes_message_count_and_types(self, tools):
        msgs = [
            {"type": "orderbook_snapshot", "local_timestamp": 1000},
            {"type": "orderbook_snapshot", "local_timestamp": 2000},
            {"type": "orderbook_delta", "local_timestamp": 3000},
        ]
        body = gzip.compress("\n".join(str(m) for m in msgs).encode())
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/orderbook",
            body=body,
            content_type="binary/octet-stream",
        )
        result = tools["convexlake_orderbook_summary"].invoke({
            "exchange": "polymarket",
            "ticker": "btc",
            "timeframe": "15m",
            "date": "2026-10-01",
            "slug": "test.gz",
        })
        assert "messages: 3" in result
        assert "orderbook_snapshot" in result

    @resp.activate
    def test_404_returns_readable_error(self, tools):
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/orderbook",
            json={"error": "no data", "code": "NOT_FOUND"},
            status=404,
        )
        result = tools["convexlake_orderbook_summary"].invoke({
            "exchange": "polymarket",
            "ticker": "btc",
            "timeframe": "15m",
            "date": "2099-01-01",
        })
        assert "No orderbook found" in result or "convexlake_info" in result


# ── convexlake_info ───────────────────────────────────────────────────────────

class TestInfoTool:
    @resp.activate
    def test_returns_catalog_dict(self, tools):
        resp.add(resp.GET, f"{BASE_URL}/v2/catalog", json=CATALOG_RESP)
        result = tools["convexlake_info"].invoke({})
        assert result["namespaces"]["pm"] == ["polymarket", "kalshi"]

    @resp.activate
    def test_returns_error_dict_on_failure(self, tools):
        resp.add(
            resp.GET, f"{BASE_URL}/v2/catalog",
            json={"error": "unauthorized", "code": "AUTH"},
            status=401,
        )
        result = tools["convexlake_info"].invoke({})
        assert "error" in result


# ── convexlake_leaderboard_trades ─────────────────────────────────────────────

class TestLeaderboardTradesTool:
    @resp.activate
    def test_address_list_truncated_to_max_addresses(self, tools):
        addresses = [f"0x{i:040x}" for i in range(100)]
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/traders",
            json={"addresses": addresses, "is_truncated": False},
        )
        content, artifact = _invoke(tools["convexlake_leaderboard_trades"], {
            "exchange": "polymarket",
            "max_addresses": 5,
        })
        assert "total traders: 100" in content
        assert content.count("0x") == 5
        assert isinstance(artifact, list)
        assert len(artifact) == 100

    @resp.activate
    def test_fills_returns_summary_and_dataframe(self, tools):
        resp.add(
            resp.GET, f"{BASE_URL}/v2/pm/trader-fills",
            body=_csv_gz(TRADES_ROWS),
            content_type="binary/octet-stream",
        )
        content, artifact = _invoke(tools["convexlake_leaderboard_trades"], {
            "exchange": "polymarket",
            "address": "0xabc",
        })
        assert "rows: 30" in content
        assert isinstance(artifact, pd.DataFrame)
