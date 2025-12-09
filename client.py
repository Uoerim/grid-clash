# client.py
"""
Grid Clash Client - Phase 2 (Events + ACK + Retries + Latency/Jitter logging)

- Sends JOIN to server.
- Receives JOIN_ACK and stores player_id.
- Receives SNAPSHOT packets silently (no printing),
  but logs server->client latency and jitter to CSV.
- Sends ACQUIRE_REQUEST(row, col) with event_id.
- Retries events until EVENT_ACK is received or max retries reached.
"""

import socket
import struct
import threading
import time
import csv
import os

from protocol import (
    MsgType,
    EventType,
    AckType,
    pack_header,
    unpack_header,
    pack_event,
    unpack_ack,
    HEADER_SIZE,
    current_millis,
)

SERVER_HOST = "127.0.0.1"
SERVER_PORT = 50000

RETRY_INTERVAL_MS = 200
MAX_RETRIES = 3

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)


# ---------- Snapshot decode ----------
def decode_snapshot_payload(payload):
    """
    Mirrors build_snapshot_payload in server.py.
    We decode just enough to validate; no printing.
    """
    offset = 0

    if len(payload) < 2:
        return None, None, None

    N, num_players = struct.unpack_from("!B B", payload, offset)
    offset += 2

    # skip players
    player_struct_size = struct.calcsize("!B f f")
    offset += num_players * player_struct_size

    # skip grid
    cells_needed = N * N
    offset += cells_needed

    return N, num_players, None


def init_client_snapshot_csv(csv_path):
    if not os.path.exists(csv_path):
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp_ms",
                "snapshot_id",
                "server_ts_ms",
                "client_rx_ms",
                "latency_ms",
                "jitter_ms",
            ])


def log_client_snapshot(csv_path, snapshot_id, server_ts_ms, client_rx_ms,
                        latency_ms, jitter_ms):
    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            current_millis(),
            snapshot_id,
            server_ts_ms,
            client_rx_ms,
            latency_ms,
            jitter_ms,
        ])


# ---------- Receiver thread ----------
def recv_loop(sock, state):
    """
    Listens for JOIN_ACK, SNAPSHOT, and ACK packets.
    SNAPSHOT packets are processed silently but logged to CSV.
    """
    while True:
        data, addr = sock.recvfrom(4096)
        if not data:
            continue

        try:
            header = unpack_header(data)
        except ValueError:
            continue

        msg_type = header["msg_type"]
        payload_len = header["payload_len"]
        payload = data[HEADER_SIZE:HEADER_SIZE + payload_len]

        if msg_type == MsgType.JOIN_ACK:
            if payload_len >= 1:
                (player_id,) = struct.unpack("!B", payload)
                state["player_id"] = player_id
                print(f"[CLIENT] JOIN_ACK received → player_id={player_id}")

        elif msg_type == MsgType.SNAPSHOT:
            snapshot_id = header["snapshot_id"]
            server_ts_ms = header["server_timestamp"]
            client_rx_ms = current_millis()

            # Ignore old/duplicate snapshots
            if snapshot_id <= state["last_applied_snapshot"]:
                continue

            state["last_applied_snapshot"] = snapshot_id

            # decode payload (basic validation)
            decode_snapshot_payload(payload)

            # latency & jitter
            latency_ms = client_rx_ms - server_ts_ms
            if latency_ms < 0:
                latency_ms = 0

            last_lat = state["last_snapshot_latency_ms"]
            if last_lat is None:
                jitter_ms = 0
            else:
                jitter_ms = abs(latency_ms - last_lat)

            state["last_snapshot_latency_ms"] = latency_ms

            log_client_snapshot(
                state["snapshot_csv"],
                snapshot_id,
                server_ts_ms,
                client_rx_ms,
                latency_ms,
                jitter_ms,
            )

        elif msg_type == MsgType.ACK:
            try:
                ack = unpack_ack(payload)
            except ValueError:
                continue

            if ack["ack_type"] == AckType.EVENT_ACK:
                ev_id = ack["event_id"]
                pending = state["pending_events"]
                if ev_id in pending:
                    del pending[ev_id]
                    print(f"[CLIENT] EVENT_ACK received for event_id={ev_id}")


# ---------- Retry thread ----------
def retry_loop(sock, state):
    """
    Periodically checks pending events and resends them if needed.
    """
    while True:
        time.sleep(0.05)
        now = current_millis()

        pending = state["pending_events"]
        # copy keys to avoid changing dict during iteration
        for ev_id in list(pending.keys()):
            info = pending.get(ev_id)
            if info is None:
                continue

            if now - info["last_send_ms"] < RETRY_INTERVAL_MS:
                continue

            if info["retries"] >= MAX_RETRIES:
                print(f"[CLIENT] EVENT {ev_id} failed after {MAX_RETRIES} retries")
                del pending[ev_id]
                continue

            # resend
            payload = pack_event(
                EventType.ACQUIRE_REQUEST,
                ev_id,
                info["row"],
                info["col"],
            )
            hdr = pack_header(
                msg_type=MsgType.EVENT,
                snapshot_id=0,
                seq_num=0,
                payload_len=len(payload),
            )
            sock.sendto(hdr + payload, (SERVER_HOST, SERVER_PORT))
            info["last_send_ms"] = now
            info["retries"] += 1
            print(f"[CLIENT] Resent EVENT {ev_id} (retry {info['retries']})")


# ---------- Send helpers ----------
def send_join(sock):
    hdr = pack_header(
        msg_type=MsgType.JOIN,
        snapshot_id=0,
        seq_num=0,
        payload_len=0,
    )
    sock.sendto(hdr, (SERVER_HOST, SERVER_PORT))
    print("[CLIENT] JOIN sent")


def send_acquire_request(sock, state, row, col):
    ev_id = state["next_event_id"]
    state["next_event_id"] += 1

    payload = pack_event(EventType.ACQUIRE_REQUEST, ev_id, row, col)
    hdr = pack_header(
        msg_type=MsgType.EVENT,
        snapshot_id=0,
        seq_num=0,
        payload_len=len(payload),
    )
    sock.sendto(hdr + payload, (SERVER_HOST, SERVER_PORT))

    state["pending_events"][ev_id] = {
        "row": row,
        "col": col,
        "last_send_ms": current_millis(),
        "retries": 0,
    }

    print(f"[CLIENT] EVENT {ev_id} sent → ({row}, {col})")


# ---------- Main ----------
def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))

    local_addr = sock.getsockname()
    local_port = local_addr[1]
    print("[CLIENT] Local address:", local_addr)

    snapshot_csv = os.path.join(LOG_DIR, f"client_snapshots_{local_port}.csv")
    init_client_snapshot_csv(snapshot_csv)

    state = {
        "player_id": None,
        "last_applied_snapshot": -1,
        "last_snapshot_latency_ms": None,
        "next_event_id": 1,
        "pending_events": {},
        "snapshot_csv": snapshot_csv,
    }

    send_join(sock)

    # background receiver + retry threads
    t_recv = threading.Thread(target=recv_loop, args=(sock, state), daemon=True)
    t_recv.start()

    t_retry = threading.Thread(target=retry_loop, args=(sock, state), daemon=True)
    t_retry.start()

    print("[CLIENT] Type: row col   to acquire a cell")
    print("[CLIENT] Type: q         to quit")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[CLIENT] Exiting.")
            break

        if not line:
            continue
        if line.lower() in ("q", "quit", "exit"):
            break

        parts = line.split()
        if len(parts) != 2:
            print("[CLIENT] Use format: row col")
            continue

        try:
            row = int(parts[0])
            col = int(parts[1])
        except ValueError:
            print("[CLIENT] row/col must be integers")
            continue

        send_acquire_request(sock, state, row, col)

    sock.close()


if __name__ == "__main__":
    main()
