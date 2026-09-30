# CryptoViz — Cryptocurrency Visualization Platform

![CryptoViz](https://img.shields.io/badge/CryptoViz-Cryptocurrency%20Visualization-blue)
![Python](https://img.shields.io/badge/Python-3.14-blue)
![uv](https://img.shields.io/badge/packaging-uv-orange)
![Flask](https://img.shields.io/badge/Flask-3.1-green)
![Chart.js](https://img.shields.io/badge/Chart.js-Latest-orange)

CryptoViz brings live prices, historical trends, volatility, correlation and
market sentiment for the top cryptocurrencies into one interactive dashboard. It
scrapes market data, stores it in SQLite, and serves both the REST API and the
frontend from a single Flask application.

![Top Gainers](https://github.com/iPriyadarshi/CryptoViz/blob/main/docs/images/top-gainers-cryptoviz.png)

## Features

- **Live price tracking** for the top 10 cryptocurrencies by market cap
- **Historical charts** over 24 h / 7 d / 1 month
- **Normalized trend comparison** — compare assets on a common scale
- **Volatility metrics** — return dispersion and maximum drawdown
- **Correlation matrix** — how assets move relative to one another
- **Market sentiment** — crypto news and Reddit scored with VADER, blended with the Fear & Greed Index
- **Top gainers** over the last 24 hours
- **Light and dark themes**

## Quick start

Requires [uv](https://docs.astral.sh/uv/) — it manages the Python version and
the dependencies, so nothing else needs installing.

```bash
git clone https://github.com/iPriyadarshi/CryptoViz.git
cd CryptoViz

cp .env.example .env      # every value has a working default
uv sync                   # creates .venv from uv.lock

uv run cryptoviz-worker   # terminal 1: scrapers
uv run cryptoviz-web      # terminal 2: web server
```

Open <http://127.0.0.1:5000>. The database (`crypto.db`) and its schema are
created automatically.

The dashboard loads immediately; charts fill in once the worker's first scrape
lands. To seed data without waiting:

```bash
uv run cryptoviz-scrape-once   # one round of every scraper
uv run cryptoviz-backfill      # 31 days of price history (several minutes)
```

### Commands

| Command | Purpose |
|---|---|
| `uv run cryptoviz-web` | Development web server |
| `uv run cryptoviz-worker` | Background scraper process |
| `uv run cryptoviz-scrape-once` | One round of every scraper, then exit |
| `uv run cryptoviz-backfill` | Backfill price history (optional day count) |
| `uv run cryptoviz-rebuild-rollups` | Rebuild the price rollups after a restore |
| `uv run pytest` | Test suite |
| `uv run ruff check src tests` | Lint |

## Project structure

```
CryptoViz/
├── pyproject.toml            # project metadata, dependencies, tooling config
├── uv.lock                   # exact pinned versions (committed)
├── src/cryptoviz/
│   ├── config.py             # every environment variable, read in one place
│   ├── app.py                # Flask application factory
│   ├── wsgi.py               # gunicorn entry point
│   ├── worker.py             # scraper process entry point
│   ├── cli.py                # console commands
│   ├── api/                  # HTTP layer — one blueprint per area
│   │   ├── pages.py          #   frontend routes + health check
│   │   ├── crypto.py         #   prices, history, volatility, correlation
│   │   ├── sentiment.py      #   sentiment endpoints
│   │   └── top_gainers.py    #   top gainers endpoints
│   ├── services/
│   │   ├── analytics.py      # history, volatility, correlation
│   │   └── updater.py        # background collection loops
│   ├── scrapers/             # one module per external source
│   │   ├── base.py           #   shared HTTP + parsing helpers
│   │   ├── prices.py         #   CoinMarketCap → CoinGecko
│   │   ├── top_gainers.py    #   CoinMarketCap → CoinGecko
│   │   ├── sentiment.py      #   news, Reddit, Fear & Greed
│   │   └── historical.py     #   one-off backfill
│   ├── db/
│   │   ├── models.py         # table definitions
│   │   ├── engine.py         # connection + SQLite tuning
│   │   └── repository.py     # all SQL
│   ├── static/               # css, js, images
│   └── templates/            # HTML pages
├── tests/
├── deploy/                   # systemd units, nginx config, scripts
└── docs/DEPLOYMENT.md        # EC2 deployment guide
```

Each layer depends only on the ones below it: `api` → `services` → `scrapers` /
`db` → `config`.

## Architecture

Two processes share one database:

```
     ┌─────────────────┐         ┌──────────────────┐
     │  cryptoviz-web  │         │ cryptoviz-worker │
     │  (gunicorn)     │         │  price loop      │
     │  API + frontend │         │  sentiment loop  │
     └────────┬────────┘         │  gainers loop    │
              │                  └────────┬─────────┘
              │     reads               writes
              └────────────┬─────────────┘
                           ▼
                     crypto.db (SQLite, WAL)
```

The scrapers deliberately run **outside** the web process. gunicorn runs several
web workers; loops living inside the app would be duplicated once per worker and
every scrape would run N times over. Keeping them separate means the schedule is
honoured exactly once, and serving and collecting can be restarted independently.

Both processes open the same SQLite file, so the engine enables WAL journalling
(readers never block on the writer) and a busy timeout (a blocked writer waits
instead of failing).

### Data sources

| Data | Primary | Fallback |
|---|---|---|
| Prices | CoinMarketCap | CoinGecko API |
| Top gainers | CoinMarketCap | CoinGecko API |
| News sentiment | CoinDesk, Cointelegraph, CryptoNews | RSS feeds |
| Social sentiment | Reddit r/CryptoCurrency | — |
| Fear & Greed Index | alternative.me | neutral (50) |

Overall sentiment is a weighted blend: **50 % news + 30 % Reddit + 20 % Fear &
Greed**. Every scraper degrades gracefully — a failed source logs a warning and
falls through rather than taking the run down.

### Database schema

| Table | Contents |
|---|---|
| `crypto_prices` | Normalized time-series price rows (history, volatility, correlation) |
| `price_buckets` | Hourly and daily averages of the above, maintained by the worker |
| `sentiment_snapshots` | Aggregate sentiment readings, one row per scrape |
| `sentiment_sources` | The articles and posts behind those scores, stored once each |
| `sentiment_source_mentions` | Which cryptocurrencies each source mentions |
| `top_gainers_snapshots` | One JSON list per top-gainers scrape |

Timestamps are stored as text in `%Y-%m-%d %H:%M:%S` — zero-padded and
most-significant-first, so lexicographic comparison is also chronological, which
lets time-range filters run as plain SQL string comparisons.

**A full year of history is retained** for all three kinds of data, and every
window is configurable.

Sources live in their own table rather than inside each snapshot because
successive scrapes re-read the same front pages, so the same article would be
stored again every five minutes. Measured over ~2,000 simulated scrapes,
100,000 source-sightings collapse to 299 unique rows and the stored data drops
from 22.5 MB to 0.5 MB — roughly **3 GB → 25 MB** extrapolated to a full year.

### Long-range charts

A year of 5-minute readings is ~105,000 points per symbol, so windows longer
than a week are served from **pre-aggregated rollups** rather than re-aggregated
on every request. `price_buckets` holds hourly and daily averages of
`crypto_prices`; the worker rebuilds it at startup and refreshes a two-day
trailing window after each scrape. Reads fall back to aggregating raw rows when
the rollup has not been built yet, so a fresh deployment is correct immediately,
and a test asserts the two paths return identical numbers.

Measured against a seeded 1,051,200-row database (149 MB):

| Request | Before rollups | After |
|---|---|---|
| `history?days=1` | 17 ms | **4 ms** |
| `history?days=365` | 150 ms | **4 ms** (366 points, 15 KB — not 105,120) |
| `correlation?days=30` | 146 ms | **20 ms** |
| `correlation?days=365` | 1778 ms | **13 ms** |
| `volatility?days=90` | 477 ms | **54 ms** |
| `volatility?days=365` | 1639 ms | **48 ms** |

Every endpoint is now under 60 ms. Two details carry most of that win:

- **Index column order.** The expression indexes lead with the truncated
  timestamp and put `symbol` second. Leading with `symbol` instead forces SQLite
  to scan all ~1,000,000 index entries to emit a few hundred recent buckets —
  774 ms versus 3 ms for the same result.
- **Filtering on the bucket expression** rather than the raw timestamp, so the
  incremental refresh is a range search over that index instead of a full scan.

Maintenance costs: a full rebuild is ~3.3 s (startup only), and the per-scrape
incremental refresh is ~255 ms in the background worker.

## API

| Endpoint | Description |
|---|---|
| `GET /api/crypto` | Latest data for all tracked cryptocurrencies |
| `GET /api/crypto/{symbol}/history` | Historical prices (`?days=1\|7\|14\|30\|90\|365`) |
| `GET /api/crypto/{symbol}/volatility` | Volatility metrics for one cryptocurrency |
| `GET /api/crypto/volatility` | Volatility for all cryptocurrencies |
| `GET /api/crypto/correlation` | Correlation matrix |
| `GET /api/sentiment` | Latest complete sentiment snapshot |
| `GET /api/sentiment/overall` | Overall market sentiment (`?days=1\|7\|30`) |
| `GET /api/sentiment/rankings` | Most positive / negative by sentiment |
| `GET /api/sentiment/sources` | Raw sources (`?type=`, `?crypto=`, `?limit=`, `?days=`) |
| `GET /api/sentiment/trends` | Sentiment plotted against price over time |
| `GET /api/top-gainers` | Latest top gainers |
| `GET /api/top-gainers/update` | Trigger a top gainers refresh |
| `GET /api/sentiment/update` | Trigger a sentiment refresh |
| `GET /healthz` | Liveness probe |

Endpoints taking `days` accept 1, 7, 14, 30, 90 or 365; anything else falls back
to the default. A browsable summary is served at `/api-docs`.

## Configuration

All configuration is environment variables, read in `src/cryptoviz/config.py` and
documented in `.env.example`. Every value has a working default.

| Variable | Default | Purpose |
|---|---|---|
| `FLASK_DEBUG` | `false` | Debug mode — never enable on a public host |
| `FLASK_HOST` / `FLASK_PORT` | `127.0.0.1` / `5000` | Dev server bind address |
| `CORS_ORIGINS` | `*` | Origins allowed to call `/api/*` |
| `CRYPTOVIZ_DB_PATH` | `./crypto.db` | SQLite file location |
| `CRYPTOVIZ_DB_BUSY_TIMEOUT` | `30` | Seconds to wait for a competing writer |
| `CRYPTOVIZ_DB_MMAP_BYTES` | `256 MiB` | Bytes of the database to memory-map |
| `CRYPTOVIZ_DB_CACHE_KIB` | `64 MiB` | SQLite page cache per connection |
| `CRYPTOVIZ_PRICE_INTERVAL` | `300` | Seconds between price scrapes |
| `CRYPTOVIZ_SENTIMENT_INTERVAL` | `300` | Seconds between sentiment scrapes |
| `CRYPTOVIZ_TOP_GAINERS_INTERVAL` | `300` | Seconds between top-gainers scrapes |
| `CRYPTOVIZ_PRICE_RETENTION_DAYS` | `365` | Price history retention |
| `CRYPTOVIZ_SENTIMENT_RETENTION_DAYS` | `365` | Sentiment retention |
| `CRYPTOVIZ_TOP_GAINERS_RETENTION_DAYS` | `365` | Top gainers retention |
| `CRYPTOVIZ_HOURLY_BUCKET_DAYS` | `7` | Windows longer than this are averaged hourly |
| `CRYPTOVIZ_DAILY_BUCKET_DAYS` | `90` | Windows longer than this are averaged daily |
| `CRYPTOVIZ_LOG_LEVEL` | `INFO` | Log verbosity |

## Deployment

See **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** for the full EC2 guide. The
short version, on a fresh Ubuntu instance:

```bash
git clone https://github.com/iPriyadarshi/CryptoViz.git
cd CryptoViz
sudo bash deploy/bootstrap.sh
```

That provisions nginx, uv, a service user, both systemd units and the database
directory. Subsequent deploys are `sudo bash deploy/update.sh`.

## Development

```bash
uv sync                          # includes dev dependencies
uv run pytest                    # 65 tests
uv run ruff check src tests      # lint
uv run ruff format src tests     # format
```

Tests run against a temporary SQLite database and stub out every network call,
so the suite is fast and works offline.

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes
4. Push and open a Pull Request

## License

MIT — see [LICENSE](LICENSE).

---

Built with ❤️ for cryptocurrency enthusiasts and data visualization lovers.
