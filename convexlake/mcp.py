"""
MCP tools for the ConvexLake SDK via the hosted MCP server.

Install:
    pip install convexlake[mcp]

Usage:
    import asyncio
    from convexlake.mcp import get_mcp_tools

    tools = asyncio.run(get_mcp_tools(api_key="do_..."))

Auth note
---------
The ConvexLake MCP server at https://api.convexlake.com/mcp uses OAuth 2.1
with dynamic client registration (RFC 7591). API-key auth (Bearer or x-api-key)
is NOT accepted by the current server — both return HTTP 401. Headless key auth
requires a server-side change.

Until that is added, you can still use this helper by passing a valid OAuth
access token via the ``headers`` argument:

    tools = asyncio.run(get_mcp_tools(
        headers={"Authorization": "Bearer <oauth_access_token>"}
    ))

For data access and analysis in an agentic workflow, prefer the SDK tools from
``convexlake.langchain`` — they work with the API key directly and return the
data, not presigned download URLs.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.tools import BaseTool


async def get_mcp_tools(
    api_key: str | None = None,
    url: str = "https://api.convexlake.com/mcp",
    headers: dict | None = None,
) -> list[BaseTool]:
    """
    Load tools from the ConvexLake MCP server via langchain-mcp-adapters.

    Parameters
    ----------
    api_key : str, optional
        Ignored — the MCP server does not currently accept API-key auth.
        Provided for API symmetry; will work once the server adds key support.
    url : str
        MCP server URL. Defaults to https://api.convexlake.com/mcp.
    headers : dict, optional
        HTTP headers to send with every MCP request. Pass an OAuth access token:
        ``{"Authorization": "Bearer <oauth_access_token>"}``.

    Returns
    -------
    list[BaseTool]

    Notes
    -----
    MCP tools return presigned S3 download URLs and catalog metadata.
    Use ``convexlake.langchain.get_tools()`` when the agent needs to analyse
    data directly (it fetches and summarises the data itself).

    The MCP server requires OAuth 2.1 (RFC 7591). API-key auth returns HTTP 401.
    """
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError:
        raise ImportError(
            "MCP support requires langchain-mcp-adapters. "
            "Install with: pip install convexlake[mcp]"
        )

    if headers is None:
        headers = {}

    # API-key auth is not yet supported by the MCP server.
    # We accept api_key for future compatibility but do not use it.
    _ = api_key or os.environ.get("CONVEXLAKE_API_KEY")

    client = MultiServerMCPClient(
        {
            "convexlake": {
                "transport": "streamable_http",
                "url": url,
                "headers": headers,
            }
        }
    )
    async with client:
        tools = client.get_tools()
    return tools
