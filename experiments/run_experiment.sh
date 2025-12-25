#!/usr/bin/env bash
#
# experiments/run_experiment.sh
#
# Usage:
#   chmod +x experiments/run_experiment.sh
#   ./experiments/run_experiment.sh baseline
#   ./experiments/run_experiment.sh loss5
#   ./experiments/run_experiment.sh delay100
#   ./experiments/run_experiment.sh delay_jitter
#   ./experiments/run_experiment.sh rate_limit
#
# Runs server + N clients for a fixed duration under a chosen netem scenario.
# Saves ALL outputs under: results/<scenario>_<timestamp>/
#   - server.log, client_*.log
#   - logs/*.csv (server_metrics.csv, server_events.csv, client_snapshots_*.csv)
#   - plots/*.png (auto-generated)

set -euo pipefail

SCENARIO="${1:-baseline}"
DURATION="${DURATION:-30}"      # seconds per run (override: DURATION=60 ./... baseline)
NUM_CLIENTS="${NUM_CLIENTS:-2}" # override: NUM_CLIENTS=3 ./... baseline
IF="${IF:-lo}"                  # interface for netem (lo if using loopback)

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS_DIR="${PROJECT_ROOT}/results"
mkdir -p "$RESULTS_DIR"

TS="$(date +'%Y%m%d_%H%M%S')"
OUT_DIR="${RESULTS_DIR}/${SCENARIO}_${TS}"
mkdir -p "$OUT_DIR"

# Per-run logs dir for CSVs written by server/client
export LOG_DIR="${OUT_DIR}/logs"
mkdir -p "${LOG_DIR}"

echo "[RUN] Scenario=${SCENARIO}, duration=${DURATION}s, clients=${NUM_CLIENTS}, IF=${IF}"
echo "[RUN] Output dir: ${OUT_DIR}"
echo "[RUN] CSV log dir: ${LOG_DIR}"

# ----------------- netem helpers -----------------
cleanup_netem() {
  echo "[RUN] Cleaning netem on ${IF}"
  sudo tc qdisc del dev "${IF}" root 2>/dev/null || true
}

setup_netem() {
  cleanup_netem

  case "$SCENARIO" in
    baseline)
      echo "[RUN] Baseline (no netem)"
      ;;

    loss5)
      echo "[RUN] 5% random loss"
      sudo tc qdisc add dev "${IF}" root netem loss 5%
      ;;

    delay100)
      echo "[RUN] 100ms fixed delay"
      sudo tc qdisc add dev "${IF}" root netem delay 100ms
      ;;

    delay_jitter)
      echo "[RUN] 100ms delay ±10ms jitter"
      sudo tc qdisc add dev "${IF}" root netem delay 100ms 10ms
      ;;

    rate_limit)
      echo "[RUN] Rate limit 2mbit + 50ms delay"
      # safer two-stage qdisc
      sudo tc qdisc add dev "${IF}" root handle 1: tbf rate 2mbit burst 32k latency 400ms
      sudo tc qdisc add dev "${IF}" parent 1:1 handle 10: netem delay 50ms
      ;;

    *)
      echo "[ERR] Unknown scenario: ${SCENARIO}"
      echo "Valid: baseline | loss5 | delay100 | delay_jitter | rate_limit"
      exit 1
      ;;
  esac
}

trap cleanup_netem EXIT

# ----------------- start server + clients -----------------
setup_netem
cd "${PROJECT_ROOT}"

echo "[RUN] Starting server..."
python server.py > "${OUT_DIR}/server.log" 2>&1 &
SERVER_PID=$!
echo "[RUN] Server PID=${SERVER_PID}"

sleep 1  # let server bind

CLIENT_PIDS=()

for i in $(seq 1 "${NUM_CLIENTS}"); do
  echo "[RUN] Starting client ${i}..."
  python client.py --duration "${DURATION}" > "${OUT_DIR}/client_${i}.log" 2>&1 &
  CLIENT_PIDS+=($!)
  sleep 0.4
done

echo "[RUN] Running for ${DURATION}s..."
sleep "${DURATION}"

echo "[RUN] Stopping clients..."
for pid in "${CLIENT_PIDS[@]}"; do
  kill "$pid" 2>/dev/null || true
done

echo "[RUN] Stopping server..."
kill "${SERVER_PID}" 2>/dev/null || true

# give OS time to flush files
sleep 1

# ----------------- generate plots -----------------
echo "[RUN] Generating plots..."
mkdir -p "${OUT_DIR}/plots"
python plot_results.py --logs "${LOG_DIR}" --out "${OUT_DIR}/plots" \
  > "${OUT_DIR}/plotter.log" 2>&1 || true

echo "[RUN] Done ✅"
echo "[RUN] Outputs:"
echo "  Logs:   ${OUT_DIR}/*.log"
echo "  CSVs:   ${LOG_DIR}/*.csv"
echo "  Plots:  ${OUT_DIR}/plots/*.png"
