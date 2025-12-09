#!/bin/bash
# Baseline test: run server + 3 clients for 60s (no netem)

set -e

cd "$(dirname "$0")/.."  # go to project root

mkdir -p logs

echo "[RUN] Starting server..."
python3 server.py > logs/server_baseline.log 2>&1 &
SERVER_PID=$!

sleep 1

echo "[RUN] Starting clients..."
python3 client.py > logs/client1_baseline.log 2>&1 &
CLIENT1_PID=$!

python3 client.py > logs/client2_baseline.log 2>&1 &
CLIENT2_PID=$!

python3 client.py > logs/client3_baseline.log 2>&1 &
CLIENT3_PID=$!

echo "[RUN] Letting them run for 60 seconds..."
sleep 60

echo "[RUN] Stopping processes..."
kill $SERVER_PID || true
kill $CLIENT1_PID $CLIENT2_PID $CLIENT3_PID 2>/dev/null || true

# just in case any stray client is still running:
pkill -f "client.py" 2>/dev/null || true

echo "[RUN] Done. Check logs/ for output and server_metrics.csv."
