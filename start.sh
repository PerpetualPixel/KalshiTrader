#!/usr/bin/env bash
# One-shot launcher for KalshiTrader on macOS / Linux: sets up, then runs the bot and dashboard.
#
#   ./start.sh            set up if needed, run dashboard + bot (Ctrl-C stops both)
#   ./start.sh --setup    install and configure only
#   ./start.sh --scan     read-only scan, prints signals, places nothing
#   ./start.sh --once     a single trading cycle
#   PORT=8080 ./start.sh  dashboard on another port
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }

# ---- 1. Python -------------------------------------------------------------
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then echo "Python 3.10+ not found. Install it and re-run." >&2; exit 1; fi

# ---- 2. Virtual environment + install -------------------------------------
if [ ! -x .venv/bin/python ]; then step "Creating virtual environment in .venv"; "$PY" -m venv .venv; fi
VPY=.venv/bin/python
STAMP=.venv/.kalshitrader-install-stamp
if ! "$VPY" -c 'import kalshitrader.cli' 2>/dev/null || [ ! -f "$STAMP" ] || [ pyproject.toml -nt "$STAMP" ]; then
  step "Installing KalshiTrader and its dependencies"
  "$VPY" -m pip install --quiet --upgrade pip
  "$VPY" -m pip install --quiet -e ".[dev,ai]"
  touch "$STAMP"
fi

# ---- 3. Config -------------------------------------------------------------
if [ ! -f .env ]; then cp .env.example .env; step "Created .env (demo API, paper trading). Edit it to change limits or add keys."; fi
mkdir -p data

case "${1:-}" in
  --setup) step "Setup complete. Run ./start.sh to launch."; exit 0 ;;
  --scan)  exec "$VPY" -m kalshitrader scan --all ;;
  --once)  exec "$VPY" -m kalshitrader run --once ;;
esac

# ---- 4. Dashboard in the background, bot in the foreground ----------------
step "Starting dashboard on http://127.0.0.1:$PORT"
"$VPY" -m kalshitrader dashboard --port "$PORT" &
DASH=$!
trap 'kill $DASH 2>/dev/null || true' EXIT
sleep 2
if command -v xdg-open >/dev/null 2>&1; then xdg-open "http://127.0.0.1:$PORT" >/dev/null 2>&1 || true
elif command -v open >/dev/null 2>&1; then open "http://127.0.0.1:$PORT" || true; fi

step "Starting the trading loop (paper mode unless .env says otherwise). Ctrl-C to stop."
echo "Tip: the bot starts OFF. Add your keys in the dashboard (Settings -> API keys), then press Resume."
exec "$VPY" -m kalshitrader run
