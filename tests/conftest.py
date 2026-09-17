import pytest
from convexlake import ConvexLake

API_KEY = "do_IvpeyIJW_kEQvB59r_9zL8TA9k_JCP7yfrb"


@pytest.fixture(scope="session")
def client():
    return ConvexLake(api_key=API_KEY)


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
    Walk the info() hierarchy to find a (date, slug) pair.

    data_type: optional key hint for the file listing level (e.g. "candles", "trades").
    Returns (date, slug) or skips the test if nothing is available.
    """
    info = client.info(exchange=exchange, ticker=ticker)
    date = pick_first(info, "dates", "date")
    if not date:
        pytest.skip(f"No dates available for {exchange}/{ticker}")

    slugs_info = client.info(exchange=exchange, ticker=ticker, date=date)
    slug = None
    if data_type:
        slug = pick_first(slugs_info, data_type)
    if not slug:
        slug = pick_first(slugs_info, "files", "slugs", "candles", "trades", "orderbook")
    if not slug:
        pytest.skip(f"No slugs available for {exchange}/{ticker}/{date}")

    return date, slug
