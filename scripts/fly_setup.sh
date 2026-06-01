#!/usr/bin/env bash
# One-time Fly.io setup for paper trading (run from repo root).
set -euo pipefail

APP="${FLY_APP:-kalshi-trader-crimson-darkness-7593}"
REGION="${FLY_REGION:-sjc}"

echo "==> App: $APP (region $REGION)"

if ! fly volumes list -a "$APP" 2>/dev/null | grep -q trade_data; then
  echo "==> Creating 1GB volume trade_data (required for results/positions persistence)"
  fly volumes create trade_data --region "$REGION" --size 1 -a "$APP" -y
else
  echo "==> Volume trade_data already exists"
fi

echo "==> Set secrets if not already set (Telegram optional but recommended):"
echo "    fly secrets set TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... -a $APP"

echo "==> Deploying..."
fly deploy -a "$APP"

echo ""
echo "==> Dashboard: https://${APP}.fly.dev/"
echo "==> Logs: fly logs -a $APP"
echo "==> SSH:  fly ssh console -a $APP"
echo "==> Stop: fly scale count 0 -a $APP   (or fly apps destroy)"
