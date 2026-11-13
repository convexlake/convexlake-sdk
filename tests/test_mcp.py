"""
Tests for convexlake.mcp.

Mocks MultiServerMCPClient — no real network calls.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_mcp_client(tools=None):
    """Build a mock MultiServerMCPClient context manager that returns tools."""
    mock_instance = AsyncMock()
    mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_instance.__aexit__ = AsyncMock(return_value=None)
    mock_instance.get_tools = MagicMock(return_value=tools or [])
    mock_cls = MagicMock(return_value=mock_instance)
    return mock_cls, mock_instance


class TestGetMcpTools:
    def test_missing_langchain_mcp_adapters_raises_install_hint(self, monkeypatch):
        import convexlake.mcp as mcp_mod

        async def patched(**kw):
            raise ImportError(
                "MCP support requires langchain-mcp-adapters. "
                "Install with: pip install convexlake[mcp]"
            )

        monkeypatch.setattr(mcp_mod, "get_mcp_tools", patched)
        with pytest.raises(ImportError, match="pip install convexlake\\[mcp\\]"):
            import asyncio
            asyncio.run(mcp_mod.get_mcp_tools())

    async def test_client_built_with_correct_url_and_transport(self):
        mock_tool = MagicMock()
        mock_tool.name = "convexlake_list_files"
        mock_cls, _ = _mock_mcp_client(tools=[mock_tool])

        with patch("langchain_mcp_adapters.client.MultiServerMCPClient", mock_cls):
            from convexlake.mcp import get_mcp_tools
            tools = await get_mcp_tools(url="https://api.convexlake.com/mcp")

        call_kwargs = mock_cls.call_args[0][0]
        assert "convexlake" in call_kwargs
        config = call_kwargs["convexlake"]
        assert config["transport"] == "streamable_http"
        assert config["url"] == "https://api.convexlake.com/mcp"
        assert tools == [mock_tool]

    async def test_custom_headers_passed_through(self):
        mock_cls, _ = _mock_mcp_client()
        custom_headers = {"Authorization": "Bearer my_oauth_token"}

        with patch("langchain_mcp_adapters.client.MultiServerMCPClient", mock_cls):
            from convexlake.mcp import get_mcp_tools
            await get_mcp_tools(headers=custom_headers)

        config = mock_cls.call_args[0][0]["convexlake"]
        assert config["headers"] == custom_headers

    async def test_api_key_does_not_appear_in_headers(self):
        """API key must not be sent — the server does not accept it."""
        mock_cls, _ = _mock_mcp_client()

        with patch("langchain_mcp_adapters.client.MultiServerMCPClient", mock_cls):
            from convexlake.mcp import get_mcp_tools
            await get_mcp_tools(api_key="do_secret_key")

        config = mock_cls.call_args[0][0]["convexlake"]
        headers = config.get("headers", {})
        assert "do_secret_key" not in str(headers)

    async def test_default_url_is_convexlake_mcp(self):
        mock_cls, _ = _mock_mcp_client()

        with patch("langchain_mcp_adapters.client.MultiServerMCPClient", mock_cls):
            from convexlake.mcp import get_mcp_tools
            await get_mcp_tools()

        config = mock_cls.call_args[0][0]["convexlake"]
        assert config["url"] == "https://api.convexlake.com/mcp"
