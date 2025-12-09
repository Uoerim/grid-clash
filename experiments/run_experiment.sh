#!/usr/bin/env bash
#
# run_experiment.sh
#
# Usage:
#   ./run_experiment.sh baseline
#   ./run_experiment.sh loss5
#   ./run_experiment.sh delay100
#   ./run_experiment.sh delay_jitter
#   ./run_experiment.sh rate_limit
#
# Run from: experiments/  (sibling of server.py / client.py)

set -e

SCENARIO="${1:-baseline}"      # baseline if not specified
DURATION=30                    # seconds per run
NUM_CLIENTS=2                  # you can change to 3–4
IF="lo"                        # network interface for netem (lo if using loopback)

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RESULTS_DIR="${PROJECT_ROOT}/results"
mkdir -p "$RESULTS_DIR"

TS="$(date +'%Y%m%d_%H%M%S')"
OUT_DIR="${RESULTS_DIR}/${SCENARIO}_${TS}"
mkdir -p "$OUT_DIR"

echo "[RUN] Scenario=${SCENARIO}, duration=${DURATION}s, clients=${NUM_CLIENTS}"
echo "[RUN] Results dir: ${OUT_DIR}"


# ----------------- netem helper -----------------
cleanup_netem() {
    echo "[RUN] Cleaning up netem on ${IF}"
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
            sudo tc qdisc add dev "${IF}" root handle 1: tbf rate 2mbit burst 32k latency 400ms
            sudo tc qdisc add dev "${IF}" parent 1:1 netem delay 50ms
            ;;

        *)
            echo "[RUN] Unknown scenario: ${SCENARIO}"
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
sleep 1   # let server bind

CLIENT_PIDS=()

for i in $(seq 1 "${NUM_CLIENTS}"); do
    echo "[RUN] Starting client ${i}..."
    python client.py > "${OUT_DIR}/client_${i}.log" 2>&1 &
    CLIENT_PIDS+=($!)
    sleep 0.5
done

echo "[RUN] All processes started. Running for ${DURATION}s..."
sleep "${DURATION}"

echo "[RUN] Stopping clients..."
for pid in "${CLIENT_PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
done

echo "[RUN] Stopping server..."
kill "${SERVER_PID}" 2>/dev/null || true

echo "[RUN] Done. Logs and CSVs are under: ${OUT_DIR}"
