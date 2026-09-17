# convexlake

Python SDK for the [Convex Lake](https://convexlake.com) market data API.

Convex Lake provides historical and live orderbook, trade, candle, volatility surface, and leaderboard data from prediction markets (Polymarket, Kalshi, Limitless, Predict.fun) and derivatives exchanges (Deribit, Hyperliquid).

---

## Installation

```bash
pip install convexlake
```

Or from source:

```bash
git clone https://github.com/convexlake/convexlake-sdk.git
cd convexlake-sdk
pip install -e .
```

**Requirements:** Python 3.10+, `requests>=2.31`, `pandas>=2.0`

---

## Authentication

All requests require an API key passed at construction time. Keys start with `do_`.

```python
from convexlake import ConvexLake

client = ConvexLake(api_key="do_XXXX...")
```

The key is sent as the `x-api-key` request header on every call.

---

## Quick start

```python
from convexlake import ConvexLake

client = ConvexLake(api_key="do_XXXX...")

# 1. Discover what's available
print(client.info())                              # list exchanges
print(client.info(exchange="kalshi"))             # list tickers
print(client.info(exchange="kalshi", ticker="btc"))            # list dates
print(client.info(exchange="kalshi", ticker="btc", date="2026-07-30"))  # list files

# 2. Download data
df = client.candles(exchange="kalshi", ticker="btc", date="2026-07-30",
                    slug="KXBTC-2601-prices.csv")

df = client.trades(exchange="deribit", ticker="BTC", date="2026-08-07",
                   slug="BTC-28AUG26-80000-C-trades.csv")

msgs = client.orderbook(exchange="polymarket", ticker="btc", timeframe="15m",
                        date="2026-04-23", slug="btc-updown-15m-1776943800-bbo.gz")
```

---

## Navigation: `info()`

Use `info()` to browse the data catalog. Call it progressively to drill down to individual files, then pass the `slug` you find to a data method.

```python
# Top level — list exchanges and top-level folders
client.info()
# {"tier": "pro", "allowed_exchanges": ["kalshi", "polymarket", ...],
#  "folders": ["candles/", "orderbook/", "trades/", "surface/"]}

# List tickers available for an exchange
client.info(exchange="polymarket")

# List dates that have data for a ticker
client.info(exchange="polymarket", ticker="btc")

# List individual files for a date
client.info(exchange="polymarket", ticker="btc", date="2026-07-30")
# {"files": ["btc-updown-5m-1785343200-bbo.gz", "btc-updown-5m-1785343200-book.gz", ...]}
```

---

## Methods

### `candles()`

Download OHLCV or price data as a `pd.DataFrame`.

```python
df = client.candles(
    exchange="kalshi",
    ticker="btc",
    date="2026-07-30",
    slug="KXBTC-2601-prices.csv",
)
```

**Column schemas by exchange:**

| Exchange | Columns |
|---|---|
| `kalshi` | `timestamp`, `yes_bid`, `yes_ask`, `price`, `volume` |
| `polymarket` | `timestamp`, `Yes`, `No` |
| `predict` | `slug`, `starts_at`, `ends_at`, `market_variant`, `title`, `winning_outcome`, `open`, `close`, `feed_symbol`, `outcomes` |
| `deribit` | `timestamp_ms`, `open`, `high`, `low`, `close`, `volume`, `cost` |
| `hyperliquid` | `timestamp_ms`, `open`, `high`, `low`, `close`, `volume`, `cost` |

**Deribit and Hyperliquid** use rolling per-instrument files (not partitioned by date). Pass `timeframe` and `slug` only — `ticker` and `date` are not used:

```python
df = client.candles(exchange="deribit", timeframe="15m", slug="BTC-PERPETUAL.csv")
df = client.candles(exchange="deribit", timeframe="1m",  slug="ETH-PERPETUAL.csv")
```

Available timeframes: `1m`, `5m`, `15m`, `30m`, `1h`

---

### `trades()`

Download trade history as a `pd.DataFrame`.

```python
df = client.trades(
    exchange="deribit",
    ticker="BTC",
    date="2026-08-07",
    slug="BTC-28AUG26-80000-C-trades.csv",
)
```

**Column schemas by exchange:**

| Exchange | Columns |
|---|---|
| `kalshi` | `created_time`, `yes_price`, `no_price`, `count`, `taker_side`, `taker_outcome_side`, `is_block_trade` |
| `polymarket` | `timestamp`, `price`, `size`, `side`, `outcome`, `transaction_hash` |
| `deribit` | `timestamp_ms`, `trade_id`, `trade_seq`, `instrument_name`, `direction`, `amount`, `price`, `mark_price`, `index_price`, `iv`, `liquidation` |

---

### `orderbook()`

Download orderbook messages as a `list[dict]`. Each dict is one message or snapshot event.

```python
msgs = client.orderbook(
    exchange="polymarket",
    ticker="btc",
    timeframe="15m",
    date="2026-04-23",
    slug="btc-updown-15m-1776943800-bbo.gz",
)

# msgs[0] → {"event_type": "best_bid_ask", "best_bid": "0.52", "best_ask": "0.54",
#             "timestamp": 1745416812000, "direction": "up", ...}
```

**Message formats by exchange:**

| Exchange | Format | Message types |
|---|---|---|
| `kalshi` | Concatenated Python dict literals | `orderbook_snapshot`, `orderbook_delta` |
| `polymarket` | Newline-delimited Python dict literals | `best_bid_ask`, `book`, `last_trade_price` |
| `predict` | Newline-delimited Python dict literals | Full book snapshots with `bids`/`asks` arrays |
| `limitless` | Newline-delimited JSON | Full book snapshots |

Format is auto-detected by default. You can override with `fmt="json"` or `fmt="python"`.

> **Deribit** orderbooks are CSV, not message-based. Use `orderbook_df()` instead — calling `orderbook()` with `exchange="deribit"` raises a helpful error.

---

### `orderbook_df()`

Download orderbook data as a `pd.DataFrame`. Use this for Deribit, which stores orderbook snapshots in CSV format.

```python
df = client.orderbook_df(
    exchange="deribit",
    ticker="BTC",
    date="2026-08-07",
    slug="BTC-28AUG26-80000-C-book.csv",
)
```

---

### `surface()`

Download a volatility surface snapshot as a `pd.DataFrame`. Currently supported for Deribit.

```python
df = client.surface(
    exchange="deribit",
    date="2026-09-14",
    slug="BTC-2026-09-14",
)
```

`slug` format: `"{CURRENCY}-{YYYY-MM-DD}"` — e.g. `"BTC-2026-09-14"`, `"ETH-2026-09-14"`.

**Columns:** `snapshot_ts`, `instrument_name`, `expiry`, `strike`, `option_type`, `underlying_price`, `mark_price`, `mark_iv`, `bid_price`, `ask_price`, `mid_price`, `volume`, `volume_usd`, `open_interest`, `estimated_delivery_price`

---

### `leaderboard()`

Download a daily leaderboard snapshot as a `pd.DataFrame`.

```python
df = client.leaderboard(exchange="polymarket", date="2026-09-14")
df = client.leaderboard(exchange="limitless",  date="2026-09-14")
```

**Column schemas by exchange:**

| Exchange | Columns |
|---|---|
| `polymarket` | `fetched_at`, `time_period`, `order_by`, `category`, `rank`, `proxyWallet`, `userName`, `xUsername`, `verifiedBadge`, `vol`, `pnl` |
| `limitless` | `fetched_at`, `metric`, `rank`, `account`, `username`, `displayName`, `positionSize_usd`, `unrealizedPnl_usd`, `heldShares`, `toWin_usd`, `mark_price`, `mark_source`, `side`, `outcomeLabel`, `market_id`, `market_title`, `market_category` |

---

### `leaderboard_trades()`

Two-step access to per-trader fill history.

**Step 1** — list available trader addresses:

```python
result = client.leaderboard_trades(exchange="polymarket")
# {"addresses": ["0xabc...", "0xdef...", ...]}  (up to 1000 addresses)
addresses = result["addresses"]
```

**Step 2** — fetch one trader's fills:

```python
df = client.leaderboard_trades(exchange="polymarket", address="0xabc...")
```

**Columns (Polymarket):** `proxyWallet`, `side`, `asset`, `conditionId`, `size`, `price`, `timestamp`, `title`, `slug`, `outcome`, `outcomeIndex`, `name`, `transactionHash`

Supported exchanges: `polymarket`, `limitless`

---

## Error handling

All exceptions inherit from `ConvexLakeError`.

```python
from convexlake import (
    ConvexLakeError,
    AuthError,       # 401 — invalid or missing API key
    PlanError,       # 403 — endpoint requires a higher subscription tier
    NotFoundError,   # 404 — no data for the requested parameters
    RateLimitError,  # 429 — rate limit exceeded after all retries
)

try:
    df = client.candles(exchange="kalshi", ticker="btc", date="2026-07-30",
                        slug="KXBTC-2601-prices.csv")
except NotFoundError:
    print("No data for that date/slug")
except PlanError:
    print("Upgrade your plan to access this endpoint")
except AuthError:
    print("Check your API key")
except RateLimitError:
    print("Slow down — rate limit hit")
except ConvexLakeError as e:
    print(f"API error: {e}")
```

---

## Configuration

```python
client = ConvexLake(
    api_key="do_XXXX...",
    timeout=60,       # request timeout in seconds (default: 60)
    max_retries=3,    # number of retries on 429 rate-limit responses (default: 3)
)
```

On a `429` response the client waits for the `Retry-After` header value (defaulting to 2 s) and retries with exponential backoff. If all retries are exhausted, `RateLimitError` is raised.

---

## Supported exchanges

| Exchange | candles | trades | orderbook | surface | leaderboard |
|---|:---:|:---:|:---:|:---:|:---:|
| `kalshi` | ✓ | ✓ | ✓ | — | — |
| `polymarket` | ✓ | ✓ | ✓ | — | ✓ |
| `deribit` | ✓ | ✓ | ✓ (`_df`) | ✓ | — |
| `predict` | ✓ | — | ✓ | — | — |
| `limitless` | — | — | ✓ | — | ✓ |
| `hyperliquid` | ✓ | — | — | — | — |

---

## Development

```bash
git clone https://github.com/convexlake/convexlake-sdk.git
cd convexlake-sdk
pip install -e ".[dev]"
```

**Run unit tests** (no API key required — HTTP layer is mocked):

```bash
pytest tests/ -v -k "not (TestInfo or TestCandles or TestTrades or TestOrderbook or TestSurface or TestLeaderboard or TestDeribit)"
```

**Run integration tests** (requires a real API key in the environment):

```bash
export CONVEXLAKE_API_KEY="do_XXXX..."
pytest tests/ -v
```

The test suite covers decompression logic, orderbook format auto-detection, HTTP error mapping, retry behavior, and live API responses for each exchange and data type.
