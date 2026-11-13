import os

import pytest

from convexlake import ConvexLake

# Integration tests require a real API key.
# Set CONVEXLAKE_API_KEY in the environment before running them.
# Unit tests (using the `responses` mock library) run without the key.
_API_KEY = os.environ.get("CONVEXLAKE_API_KEY", "")


@pytest.fixture(scope="session")
def client():
    if not _API_KEY:
        pytest.skip(
            "Integration test skipped — set CONVEXLAKE_API_KEY to run it."
        )
    return ConvexLake(api_key=_API_KEY)


def pick_first(obj, *preferred_keys):
    """Return first scalar found in a dict or list, trying preferred keys first."""
    if isinstance(obj, list) and obj:
        return obj[0]
    if isinstance(obj, dict):
        for k in preferred_keys:
            v = obj.get(k)
            if isinstance(v, list) and v:
                return v[0]
            if isinstance(v, str) and v:
                return v
        for v in obj.values():
            if isinstance(v, list) and v:
                return v[0]
    return None


def discover(client, exchange, ticker, data_type=None):
    """
    Walk the catalog() hierarchy to find a (date, slug) pair.

    data_type: optional key hint for the file listing level (e.g. "candles", "trades").
    Returns (date, slug) or skips the test if nothing is available.
    """
    info = client.catalog(exchange=exchange, ticker=ticker)
    date = pick_first(info, "dates", "date")
    if not date:
        pytest.skip(f"No dates available for {exchange}/{ticker}")

    slugs_info = client.catalog(exchange=exchange, ticker=ticker, date=date)
    slug = None
    if data_type:
        slug = pick_first(slugs_info, data_type)
    if not slug:
        slug = pick_first(slugs_info, "files", "slugs", "candles", "trades", "orderbook")
    if not slug:
        pytest.skip(f"No slugs available for {exchange}/{ticker}/{date}")

    return date, slug
