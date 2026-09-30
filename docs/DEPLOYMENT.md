# Deploying CryptoViz on a single EC2 instance

This guide deploys the whole platform — scraper, API and frontend — onto one EC2
instance backed by a local SQLite database.

Two long-running services do the work:

| Service            | What it does                                         | Unit                                      |
| ------------------ | ---------------------------------------------------- | ----------------------------------------- |
| `cryptoviz-web`    | Serves the frontend and the JSON API via gunicorn    | `deploy/systemd/cryptoviz-web.service`    |
| `cryptoviz-worker` | Scrapes prices, sentiment and top gainers on a timer | `deploy/systemd/cryptoviz-worker.service` |

nginx sits in front, serving static assets straight off disk and proxying
everything else to gunicorn over a unix socket.

```
Internet ──▶ nginx :80 ──┬──▶ /css /js /assets ──▶ files on disk
                         └──▶ everything else ──▶ gunicorn (unix socket)
                                                       │
                                                       ▼
                                              /var/lib/cryptoviz/crypto.db
                                                       ▲
                                                       │
                                            cryptoviz-worker (scrapers)
```

> **Why two processes?** gunicorn runs several web workers. If the scrape loops
> lived inside the app, every worker would start its own copy and each scrape
> would run N times over. Splitting them means the collection schedule is
> honoured exactly once, and the two halves can be restarted independently.

---

## 1. Launch the instance

| Setting       | Recommendation                                                                                          |
| ------------- | ------------------------------------------------------------------------------------------------------- |
| AMI           | Ubuntu Server 24.04 LTS (the scripts also support Amazon Linux 2023)                                    |
| Instance type | `t3.small` — 2 GB RAM. `t3.micro` (1 GB) works but leaves little headroom for pandas during correlation |
| Storage       | 20 GB gp3. With the default 1-year retention the database settles at roughly 200 MB (measured: 1,051,200 price rows = 140 MB, plus ~25 MB of bucket indexes and ~25 MB of sentiment) |
| Key pair      | One you hold, for SSH                                                                                   |

**Security group** — inbound:

| Type  | Port | Source           | Why                                             |
| ----- | ---- | ---------------- | ----------------------------------------------- |
| SSH   | 22   | **Your IP only** | Administration. Do not open this to `0.0.0.0/0` |
| HTTP  | 80   | `0.0.0.0/0`      | Public site                                     |
| HTTPS | 443  | `0.0.0.0/0`      | Only if you set up TLS (section 7)              |

Leave outbound fully open — the worker needs to reach CoinMarketCap, CoinGecko,
Reddit and the news sites.

Nothing needs port 5000 open: gunicorn binds a unix socket, not a TCP port.

---

## 2. Connect and fetch the code

```bash
ssh -i /path/to/key.pem ubuntu@<instance-public-dns>

sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/iPriyadarshi/CryptoViz.git
cd CryptoViz
```

---

## 3. Run the bootstrap script

One command provisions everything:

```bash
sudo bash deploy/bootstrap.sh
```

It is idempotent — safe to re-run — and performs these steps:

1. Installs nginx and supporting packages.
2. Installs **uv** to `/usr/local/bin/uv`.
3. Creates the `cryptoviz` system user (no shell, no login).
4. Copies the code to `/opt/cryptoviz` and creates `/var/lib/cryptoviz` for the database.
5. Seeds `/opt/cryptoviz/.env` from `.env.example`, pointing the DB at `/var/lib/cryptoviz/crypto.db`.
6. Runs `uv sync --frozen --no-dev` — installs exactly what `uv.lock` pins.
7. Downloads the NLTK VADER lexicon to `/usr/local/share/nltk_data` so neither service reaches for the network at startup.
8. Installs and starts both systemd units, and the nginx site.

When it finishes:

```bash
curl -s localhost/healthz          # {"status": "ok"}
systemctl status cryptoviz-web cryptoviz-worker --no-pager
```

Open `http://<instance-public-dns>/` in a browser. The dashboard loads
immediately; charts stay empty until the worker's first scrape lands (within
about five minutes).

---

## 4. Review the configuration

Everything is driven by `/opt/cryptoviz/.env`. The defaults are production-ready;
the ones worth a second look:

```bash
sudo -u cryptoviz nano /opt/cryptoviz/.env
```

| Variable                         | Default                        | Notes                                                                         |
| -------------------------------- | ------------------------------ | ----------------------------------------------------------------------------- |
| `FLASK_DEBUG`                    | `false`                        | **Never** set true on a public instance — it exposes an interactive console   |
| `CRYPTOVIZ_DB_PATH`              | `/var/lib/cryptoviz/crypto.db` | Set by bootstrap                                                              |
| `CORS_ORIGINS`                   | `*`                            | Set to your domain if you don't want the API callable from other sites        |
| `CRYPTOVIZ_PRICE_INTERVAL`       | `300`                          | Seconds between price scrapes. Lowering raises the risk of being rate-limited |
| `CRYPTOVIZ_PRICE_RETENTION_DAYS` | `365`                          | Bounds both database size and the longest chart window |
| `CRYPTOVIZ_LOG_LEVEL`            | `INFO`                         | `DEBUG` is verbose — use it only while diagnosing                             |

After editing:

```bash
sudo systemctl restart cryptoviz-web cryptoviz-worker
```

---

## 5. Backfill historical data (optional)

Volatility and correlation need history. The worker accumulates it from the
moment it starts, but you can backfill 31 days from CoinGecko right away:

```bash
sudo -u cryptoviz /opt/cryptoviz/.venv/bin/cryptoviz-backfill
```

This takes **several minutes** — CoinGecko's free tier is rate-limited, so the
script deliberately paces itself. Run it inside `tmux` or `screen` if your SSH
session might drop.

To check the sources parse without leaving anything running:

```bash
sudo -u cryptoviz /opt/cryptoviz/.venv/bin/cryptoviz-scrape-once
```

---

## 5a. What a year of retention means

The default configuration keeps **a full year** of prices, sentiment and top
gainers. Measured against a seeded database at real production volume:

| | Steady state after a year |
|---|---|
| Price rows | 1,051,200 (10 symbols x 5-minute readings) |
| Price data | ~140 MB |
| Rollup table + indexes | ~35 MB (87,610 hourly + 3,660 daily buckets) |
| Sentiment | ~25 MB (snapshots ~100 bytes each; sources stored once) |
| **Total** | **~200 MB** (measured 149 MB after VACUUM) |

That comfortably fits the recommended 20 GB volume, and the backup script
gzips it to considerably less.

**Long windows are served from pre-aggregated rollups.** A year of 5-minute
data is ~105,000 points per symbol, which no chart can draw and no browser
should download. The `price_buckets` table holds hourly and daily averages; the
worker rebuilds it at startup and refreshes a two-day trailing window after each
scrape. A one-year price request returns ~366 points, about 15 KB.

Response times measured on that database:

| Request | Time |
|---|---|
| `history?days=1` | 4 ms |
| `history?days=365` | 4 ms |
| `correlation?days=30` | 20 ms |
| `correlation?days=365` | 13 ms |
| `volatility?days=365` | 48 ms |

Everything stays under 60 ms. Rollup maintenance costs ~3.3 s at worker startup
and ~255 ms per scrape, both in the background worker rather than in a request.

If the rollup is ever missing or stale — after restoring a backup, or importing
history by some route other than the backfill command — rebuild it:

```bash
sudo -u cryptoviz /opt/cryptoviz/.venv/bin/cryptoviz-rebuild-rollups
```

Restarting the worker does the same thing, since it rebuilds on startup. Reads
fall back to aggregating raw rows when no rollup exists, so the charts stay
correct meanwhile — just slower on long windows.

**Shortening retention** is a config change plus one cleanup pass:

```bash
# in /opt/cryptoviz/.env
CRYPTOVIZ_PRICE_RETENTION_DAYS=90

sudo systemctl restart cryptoviz-worker   # trims on its next cycle
```

SQLite does not return freed pages to the filesystem on its own. After a large
reduction, reclaim the space:

```bash
sudo systemctl stop cryptoviz-web cryptoviz-worker
sudo -u cryptoviz sqlite3 /var/lib/cryptoviz/crypto.db 'VACUUM;'
sudo systemctl start cryptoviz-web cryptoviz-worker
```

---

## 6. Day-to-day operations

### Logs

```bash
# Follow both services
sudo journalctl -u cryptoviz-web -u cryptoviz-worker -f

# Just the scrapers, last 100 lines
sudo journalctl -u cryptoviz-worker -n 100 --no-pager

# Errors only, since this morning
sudo journalctl -u cryptoviz-web -p err --since today
```

### Service control

```bash
sudo systemctl restart cryptoviz-web       # reload the app
sudo systemctl reload  cryptoviz-web       # graceful gunicorn reload, no dropped connections
sudo systemctl stop    cryptoviz-worker    # pause scraping (e.g. while you debug)
sudo systemctl status  cryptoviz-worker
```

### Deploying a new version

```bash
sudo bash /opt/cryptoviz/deploy/update.sh
```

Pulls, re-syncs dependencies, restarts both services and verifies the health
check — rolling back is then just `git reset` to the previous commit and running
it again. The database is untouched.

### Inspecting the database

```bash
sudo apt-get install -y sqlite3
sudo -u cryptoviz sqlite3 /var/lib/cryptoviz/crypto.db

sqlite> SELECT COUNT(*), MIN(timestamp), MAX(timestamp) FROM crypto_prices;
sqlite> SELECT symbol, COUNT(*) FROM crypto_prices GROUP BY symbol;
sqlite> SELECT created_at FROM sentiment_snapshots ORDER BY created_at DESC LIMIT 5;
```

### Backups

```bash
sudo bash /opt/cryptoviz/deploy/backup.sh
```

Uses SQLite's own `.backup`, which snapshots a live database consistently —
`cp` on a database being written can capture a torn page and, in WAL mode, miss
committed data still sitting in the `-wal` file.

Schedule it daily:

```bash
echo '0 3 * * * root bash /opt/cryptoviz/deploy/backup.sh' \
    | sudo tee /etc/cron.d/cryptoviz-backup
```

Backups land in `/var/backups/cryptoviz`, gzipped, with 14 days retained. For
off-instance durability, sync them to S3:

```bash
aws s3 sync /var/backups/cryptoviz s3://your-bucket/cryptoviz-backups/
```

---

## 7. HTTPS (recommended for a public site)

With a domain pointed at the instance's public IP:

```bash
sudo apt-get install -y certbot python3-certbot-nginx

# Put your domain in the nginx config first
sudo nano /etc/nginx/sites-available/cryptoviz   # server_name example.com;
sudo nginx -t && sudo systemctl reload nginx

sudo certbot --nginx -d example.com
```

certbot edits the nginx site to serve TLS and redirect HTTP, and installs a
renewal timer. Verify renewal works:

```bash
sudo certbot renew --dry-run
```

Then open port 443 in the security group.

---

## 8. Troubleshooting

### The site returns 502 Bad Gateway

nginx cannot reach gunicorn.

```bash
sudo systemctl status cryptoviz-web
sudo journalctl -u cryptoviz-web -n 50 --no-pager
ls -l /run/cryptoviz/gunicorn.sock        # should exist while the service runs
```

A missing socket usually means gunicorn failed at startup — the journal will
show the Python traceback.

### Charts are empty

The worker has not collected anything yet, or it is failing.

```bash
sudo journalctl -u cryptoviz-worker -n 50 --no-pager
sudo -u cryptoviz sqlite3 /var/lib/cryptoviz/crypto.db \
    'SELECT COUNT(*) FROM crypto_prices;'
```

`0` rows with `Request to https://coinmarketcap.com/ failed` in the log is
normal and _not_ fatal — the scraper falls back to CoinGecko. If the fallback
also fails, check outbound connectivity:

```bash
curl -s 'https://api.coingecko.com/api/v3/ping'
```

### "database is locked"

The web and worker processes share one SQLite file. The engine already enables
WAL and a 30-second busy timeout, so this should not surface; if it does, raise
the timeout:

```bash
# in /opt/cryptoviz/.env
CRYPTOVIZ_DB_BUSY_TIMEOUT=60
```

Confirm WAL is actually on:

```bash
sudo -u cryptoviz sqlite3 /var/lib/cryptoviz/crypto.db 'PRAGMA journal_mode;'
# expected: wal
```

### 403 Forbidden on CSS or JS

nginx cannot traverse into the static directories:

```bash
sudo chmod o+x /opt/cryptoviz /opt/cryptoviz/src /opt/cryptoviz/src/cryptoviz
sudo systemctl reload nginx
```

### Long-range charts are slow

The rollup is probably missing, so reads are falling back to aggregating raw
rows. Check and rebuild:

```bash
sudo -u cryptoviz sqlite3 /var/lib/cryptoviz/crypto.db     'SELECT granularity, COUNT(*) FROM price_buckets GROUP BY granularity;'

sudo -u cryptoviz /opt/cryptoviz/.venv/bin/cryptoviz-rebuild-rollups
```

Expect roughly 8,760 hourly and 365 daily rows per tracked symbol at full
retention. Zero rows means the worker has never completed a startup rebuild —
check `journalctl -u cryptoviz-worker` for a failure.

### The instance runs out of memory

Correlation over a long window is the heaviest operation. On a 1 GB `t3.micro`,
reduce the worker count and add swap:

```bash
# in /opt/cryptoviz/.env
GUNICORN_WORKERS=2

sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### Service won't start after a code change

```bash
sudo journalctl -u cryptoviz-web -n 80 --no-pager    # read the traceback

# Reproduce it directly, outside systemd:
cd /opt/cryptoviz
sudo -u cryptoviz .venv/bin/python -c 'from cryptoviz.wsgi import app'
```

---

## 9. What lives where

| Path                                      | Contents                                                     |
| ----------------------------------------- | ------------------------------------------------------------ |
| `/opt/cryptoviz`                          | Application code and the `.venv`                             |
| `/opt/cryptoviz/.env`                     | Configuration (mode `640`, owned by `cryptoviz`)             |
| `/var/lib/cryptoviz/crypto.db`            | The SQLite database — **the only stateful thing on the box** |
| `/var/backups/cryptoviz`                  | Local backups                                                |
| `/usr/local/share/nltk_data`              | VADER sentiment lexicon                                      |
| `/run/cryptoviz/gunicorn.sock`            | gunicorn socket (recreated on each start)                    |
| `/etc/systemd/system/cryptoviz-*.service` | Service units                                                |
| `/var/log/nginx/cryptoviz.*.log`          | nginx access and error logs                                  |

Everything except `/var/lib/cryptoviz` is reproducible from the repository, so a
backup of that one directory is a full backup of the deployment.

---

## 10. Security notes

The systemd units are hardened: both services run as an unprivileged user with
`ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges` and no writable paths
other than the database directory.

Worth doing beyond that:

- **Restrict SSH** to your own IP in the security group, or use SSM Session Manager and close port 22 entirely.
- **Keep `FLASK_DEBUG=false`.** Debug mode exposes a remote code execution console.
- **Narrow `CORS_ORIGINS`** to your domain if the API shouldn't be callable from other sites.
- **Apply updates**: `sudo apt-get update && sudo apt-get upgrade -y`, and consider `unattended-upgrades`.
- **Terminate TLS** (section 7) before treating the site as public.

The application holds no user accounts, accepts no uploads and writes no
user-supplied data to the database — all stored content comes from the scrapers.
