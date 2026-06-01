# Paper trading simulation

Autonomous strategy testing against live Kalshi market data with simulated fills, a web dashboard, and optional Telegram alerts.

## Deploy to Fly.io (recommended for 24/7)

The same `run_server.py` process runs on Fly: strategies + paper executor + **public dashboard**.

| Setting | Value |
|---------|--------|
| Run until stop | `TEST_DURATION=0` in [`fly.toml`](fly.toml) |
| Dashboard | `https://kalshi-trader-crimson-darkness-7593.fly.dev/` |
| Persistence | Volume `trade_data` → `/data` (results, positions, logs) |
| Memory | 512MB (was 256MB — too small for 6 strategies + Telegram) |

**One-time setup:**

```bash
# Install flyctl: https://fly.io/docs/hands-on/install-flyctl/
fly auth login

# Create volume + deploy (or use helper script)
chmod +x scripts/fly_setup.sh
./scripts/fly_setup.sh

# Optional Telegram (not in image — use secrets)
fly secrets set TELEGRAM_BOT_TOKEN=your_token TELEGRAM_CHAT_ID=your_chat_id \
  -a kalshi-trader-crimson-darkness-7593
```

**Operations:**

```bash
fly logs -a kalshi-trader-crimson-darkness-7593    # live logs
fly status -a kalshi-trader-crimson-darkness-7593  # machine health
fly ssh console -a kalshi-trader-crimson-darkness-7593
ls /data/results                                   # saved strategy JSON
```

Fly sends `SIGTERM` on deploy restart; the app saves results and exits cleanly. Use **exactly one machine** (in-memory paper state is not shared across replicas).

**Why not run locally?** You can — see below. Fly is for a stable URL and leaving it running without keeping your laptop open.

## Local paper simulation

Run everything in **one process**:

```bash
python run_server.py
```

This starts:

- **Paper executor** on `http://127.0.0.1:8100` (in-memory fills)
- **Dashboard** on `http://0.0.0.0:8080`
- Strategy scanners and optional Telegram hourly digests

Duration is controlled by `TEST_DURATION` in `.env` (default `86400` = 24 hours). Set `TEST_DURATION=0` to run until Ctrl+C.

## Port 8100 — do not run two executors

`run_server.py` binds the paper executor to port **8100**. Do **not** run [`executor/server.py`](executor/server.py) at the same time on the same machine unless you intentionally want live Kalshi fills instead of paper mode. A second listener will cause the paper executor to fail at startup (check logs for `Paper executor failed to bind port 8100`).

For live trading, use only `executor/server.py` and point `EXECUTOR_URL` at it from a separate monitoring process — not `run_server.py` in parallel.

## Telegram hourly digests

When `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env`:

1. **Startup status** — sent ~5 seconds after launch (uptime near `0.0h`; entries may still be `0` before the first 60s scan).
2. **Hourly status** — every 3600 seconds after that.

Digest stats use the same in-process `PaperExecutor` and `VirtualTrader.summary()` data as the dashboard (`/api/status`), not a blocking HTTP self-call to `/health`.

## Three layers

| Layer | Module | Role |
|-------|--------|------|
| **Observe** | `kalshi_client.py` | Public production API — sports markets, sorted by volume (24h) |
| **Execute** | `paper_executor.py` + `virtual_trader.py` | Simulated fills (in-process); HTTP on `:8100` for Telegram |
| **Display** | `dashboard.html` + `results/` | Live dashboard; per-strategy JSON + `run_summary_*.json` on exit |

Strategies scan markets **highest volume first** (`min_volume` in `strategy_config.py`). Autosave every 15 minutes; final save on shutdown.

Smoke test (no server required):

```bash
.venv/bin/python test_e2e_paper.py
```

## Verify dashboard vs digest

With `run_server.py` running:

```bash
curl -s http://127.0.0.1:8100/health
curl -s http://localhost:8080/api/status | python3 -m json.tool
```

Compare `balance`, `total_entries`, `total_exits`, and `total_pl` with the Telegram message.

## Other entrypoints

| Command | Use case |
|---------|----------|
| `python test_strategies.py` | Strategies only (no dashboard, no Telegram digest) |
| `python main.py` | Manual Telegram opportunity alerts (not paper simulation) |
| `python executor/server.py` | Live Kalshi executor on port 8100 |
