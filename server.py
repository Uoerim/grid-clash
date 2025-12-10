# server.py
"""
Grid Clash Server - Phase 2 (Events + ACK + Latency logging + Redundant snapshots)

- Listens on UDP.
- Handles:
    - JOIN -> JOIN_ACK (player_id)
    - EVENT(ACQUIRE_REQUEST) -> update grid cell owner + EVENT_ACK
- Periodically broadcasts SNAPSHOT packets (each sent twice for redundancy)
  to all connected clients.

Logs:
- logs/server_metrics.csv: snapshot_id + num_clients per tick
- logs/server_events.csv: per EVENT latency (client -> server)
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
    unpack_event,
    pack_ack,
    HEADER_SIZE,
    current_millis,
)

# ------------ Config ------------
SERVER_HOST = "0.0.0.0"
SERVER_PORT = 50000
UPDATE_HZ = 20          # snapshots per second
GRID_SIZE = 20          # 20x20 grid
CLIENT_TIMEOUT_MS = 15000   # 15 seconds without heartbeat to mark client offline
CLEANUP_INTERVAL_S = 1    # Check for dead clients every 1 second

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)
SERVER_CSV = os.path.join(LOG_DIR, "server_metrics.csv")
SERVER_EVENTS_CSV = os.path.join(LOG_DIR, "server_events.csv")

# ------------ Global state ------------
clients = set()         # set of (ip, port)
players = {}            # (ip, port) -> player_id
player_id_history = {}  # (ip, port) -> player_id (preserved even after timeout)
next_player_id = 1

snapshot_id = 0
seq_num = 0

# simple grid: owner_id per cell (0 = unclaimed, >0 = player_id)
grid = [0] * (GRID_SIZE * GRID_SIZE)

# per-client set of processed event_ids to avoid double-apply
processed_events = {}   # (ip, port) -> set(event_id)

# heartbeat tracking: last time we heard from each client
client_last_seen = {}   # (ip, port) -> timestamp_ms


# ------------ Logging ------------
def init_metrics_files():
    # snapshots metrics
    if not os.path.exists(SERVER_CSV):
        with open(SERVER_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp_ms", "snapshot_id", "num_clients"])

    # event latency metrics
    if not os.path.exists(SERVER_EVENTS_CSV):
        with open(SERVER_EVENTS_CSV, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp_ms",
                "player_id",
                "event_id",
                "row",
                "col",
                "client_ts_ms",
                "server_rx_ms",
                "latency_ms",
            ])


def log_snapshot(snapshot_id_val, num_clients):
    with open(SERVER_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([current_millis(), snapshot_id_val, num_clients])


def log_event_latency(player_id, ev):
    """
    ev dict keys:
      - event_id
      - row
      - col
      - client_ts_ms
    """
    server_rx_ms = current_millis()
    client_ts_ms = ev["client_ts_ms"]
    latency_ms = server_rx_ms - client_ts_ms
    if latency_ms < 0:
        latency_ms = 0

    with open(SERVER_EVENTS_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            server_rx_ms,
            player_id,
            ev["event_id"],
            ev["row"],
            ev["col"],
            client_ts_ms,
            server_rx_ms,
            latency_ms,
        ])


# ------------ Snapshot payload ------------
def build_snapshot_payload():
    """
    Encodes:
      uint8 N
      uint8 num_players
      [ per player: uint8 player_id, float32 x, float32 y ]
      [ grid cells: uint8 owner_id * (N*N) ]

    For now we fake player positions:
      x = player_id, y = 0.0
    """
    N = GRID_SIZE
    num_players = len(players)

    payload = struct.pack("!B B", N, num_players)

    # Fake positions
    for player_id in players.values():
        x = float(player_id)
        y = 0.0
        payload += struct.pack("!B f f", player_id, x, y)

    for owner in grid:
        payload += struct.pack("!B", owner)

    return payload


# ------------ Game logic ------------
def handle_acquire_request(addr, event_info):
    """
    Handle ACQUIRE_REQUEST from client 'addr'.

    event_info keys:
      - event_type
      - event_id
      - row
      - col
      - client_ts_ms
    """
    if addr not in players:
        print("[SERVER] ACQUIRE_REQUEST from unknown client", addr)
        return

    player_id = players[addr]
    row = event_info["row"]
    col = event_info["col"]

    if row < 0 or row >= GRID_SIZE or col < 0 or col >= GRID_SIZE:
        print(f"[SERVER] Invalid cell ({row}, {col}) from player {player_id}")
        return

    idx = row * GRID_SIZE + col
    current_owner = grid[idx]

    if current_owner == 0:
        grid[idx] = player_id
        print(f"[SERVER] Player {player_id} acquired cell ({row}, {col}) (idx={idx})")
    else:
        print(
            f"[SERVER] Player {player_id} tried to acquire cell ({row}, {col}) "
            f"but it is already owned by player {current_owner}"
        )


# ------------ Network handling ------------
def handle_client_messages(sock):
    """
    Receive messages from clients:
      - JOIN  -> register client and send JOIN_ACK(player_id)
      - EVENT(ACQUIRE_REQUEST) -> update grid + EVENT_ACK + log latency
    """
    global next_player_id

    print("[SERVER] Listening for client messages...")
    while True:
        try:
            data, addr = sock.recvfrom(4096)
        except ConnectionResetError:
            # Windows UDP sometimes throws this when client closes / ICMP sent.
            # We silently ignore it to avoid log spam.
            continue
        except OSError as e:
            # Socket closed or other OS-level error
            print(f"[SERVER] Socket error in handle_client_messages: {e}")
            break

        if not data:
            continue

        try:
            header = unpack_header(data)
        except ValueError as e:
            print(f"[SERVER] Bad packet from {addr}: {e}")
            continue

        msg_type = header["msg_type"]
        payload_len = header["payload_len"]
        payload = data[HEADER_SIZE:HEADER_SIZE + payload_len]

        if msg_type == MsgType.JOIN:
            # new or existing client
            if addr not in clients:
                clients.add(addr)
                # Check if this client was timed out before
                if addr in player_id_history:
                    # Restore old player_id
                    players[addr] = player_id_history[addr]
                    print(f"[SERVER] Client {addr} re-joined with old player_id {players[addr]}")
                else:
                    # Brand new client
                    players[addr] = next_player_id
                    player_id_history[addr] = next_player_id
                    print(f"[SERVER] New client {addr} -> player_id {next_player_id}")
                    next_player_id += 1
                processed_events[addr] = set()
            else:
                print(f"[SERVER] Existing client re-joined: {addr}")

            player_id = players[addr]
            client_last_seen[addr] = current_millis()

            # send JOIN_ACK with assigned player_id
            ack_payload = struct.pack("!B", player_id)
            hdr = pack_header(
                msg_type=MsgType.JOIN_ACK,
                snapshot_id=0,
                seq_num=0,
                payload_len=len(ack_payload),
            )
            sock.sendto(hdr + ack_payload, addr)

        elif msg_type == MsgType.EVENT:
            try:
                ev = unpack_event(payload)
            except ValueError as e:
                print(f"[SERVER] Bad EVENT from {addr}: {e}")
                continue

            # Update last seen timestamp (heartbeat)
            client_last_seen[addr] = current_millis()

            # If client was timed out or unknown, re-register it with same player_id
            if addr not in players:
                clients.add(addr)
                # Restore player_id from history if available
                if addr in player_id_history:
                    players[addr] = player_id_history[addr]
                    print(f"[SERVER] Client {addr} reconnected (was timed out) -> restoring player_id {players[addr]}")
                else:
                    # Brand new client (shouldn't happen normally)
                    players[addr] = next_player_id
                    player_id_history[addr] = next_player_id
                    print(f"[SERVER] New client {addr} (via EVENT) -> player_id {next_player_id}")
                    next_player_id += 1
                processed_events[addr] = set()

            ev_id = ev["event_id"]
            player_id = players.get(addr, -1)

            # Idempotent: don't process same event twice
            seen = processed_events.setdefault(addr, set())
            if ev_id in seen:
                print(f"[SERVER] Duplicate EVENT {ev_id} from {addr}, ignoring apply.")
            else:
                seen.add(ev_id)
                # game logic
                if ev["event_type"] == EventType.ACQUIRE_REQUEST:
                    handle_acquire_request(addr, ev)
                else:
                    print(f"[SERVER] Unknown EventType {ev['event_type']} from {addr}")

            # log latency (client -> server)
            if player_id != -1:
                log_event_latency(player_id, ev)

            # Always send ACK so client can stop retrying
            ack_payload = pack_ack(AckType.EVENT_ACK, ev_id)
            hdr = pack_header(
                msg_type=MsgType.ACK,
                snapshot_id=0,
                seq_num=0,
                payload_len=len(ack_payload),
            )
            sock.sendto(hdr + ack_payload, addr)

        # other msg types can be handled later


def cleanup_dead_clients():
    """
    Periodically check for clients that haven't sent any message
    (JOIN, EVENT, etc.) within CLIENT_TIMEOUT_MS.
    Removes them from clients, players, and processed_events.
    """
    print(f"[SERVER] Client cleanup thread started (timeout={CLIENT_TIMEOUT_MS}ms, interval={CLEANUP_INTERVAL_S}s)")
    
    while True:
        time.sleep(CLEANUP_INTERVAL_S)
        
        now = current_millis()
        dead_clients = []
        
        for addr in list(clients):
            last_seen = client_last_seen.get(addr, now)
            age_ms = now - last_seen
            
            if age_ms > CLIENT_TIMEOUT_MS:
                dead_clients.append(addr)
        
        for addr in dead_clients:
            clients.discard(addr)
            player_id = players.pop(addr, None)
            processed_events.pop(addr, None)
            client_last_seen.pop(addr, None)
            print(f"[SERVER] Client {addr} (player {player_id}) timed out after {CLIENT_TIMEOUT_MS}ms")


def broadcast_snapshots(sock):
    """
    Periodically sends SNAPSHOT packets to all connected clients.
    Uses simple redundancy: sends each snapshot twice with same snapshot_id.
    """
    global snapshot_id, seq_num

    interval = 1.0 / UPDATE_HZ
    print(f"[SERVER] Snapshot broadcast loop running at {UPDATE_HZ} Hz")

    while True:
        time.sleep(interval)

        if not clients:
            continue

        snapshot_id += 1
        seq_num += 1

        payload = build_snapshot_payload()
        hdr = pack_header(
            msg_type=MsgType.SNAPSHOT,
            snapshot_id=snapshot_id,
            seq_num=seq_num,
            payload_len=len(payload),
        )
        packet = hdr + payload

        # redundancy factor = 2
        current_clients = list(clients)
        for _ in range(2):  # redundancy factor = 2
            for c in current_clients:
                sock.sendto(packet, c)

        log_snapshot(snapshot_id, len(current_clients))


def main():
    init_metrics_files()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((SERVER_HOST, SERVER_PORT))
    print(f"[SERVER] UDP server on {SERVER_HOST}:{SERVER_PORT}")

    # Thread to handle incoming messages
    t = threading.Thread(target=handle_client_messages, args=(sock,), daemon=True)
    t.start()

    # Thread to cleanup dead clients
    cleanup_thread = threading.Thread(target=cleanup_dead_clients, daemon=True)
    cleanup_thread.start()

    # Main thread does broadcasting
    broadcast_snapshots(sock)


if __name__ == "__main__":
    main()
