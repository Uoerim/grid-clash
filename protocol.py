# protocol.py
"""
GCSP - Grid Clash Sync Protocol v1

Header fields (big-endian / network order):

4 bytes  protocol_id     (e.g. b'GCSP')
1 byte   version         (currently 1)
1 byte   msg_type        (see MsgType enum)
4 bytes  snapshot_id     (global snapshot counter from server)
4 bytes  seq_num         (per-connection / per-server packet sequence)
8 bytes  server_ts_ms    (server timestamp in ms since epoch)
2 bytes  payload_len     (size of payload in bytes)
4 bytes  checksum        (0 for now – reserved for future CRC)

Total header size = 28 bytes.
"""

import struct
import time
from enum import IntEnum


# ---- Constants ----

PROTOCOL_ID = b'GCSP'
VERSION = 1


class MsgType(IntEnum):
    JOIN = 0        # client -> server : join request
    JOIN_ACK = 1    # server -> client : assigned player_id
    SNAPSHOT = 2    # server -> clients : game state snapshot
    EVENT = 3       # client -> server : ACQUIRE_REQUEST etc.
    ACK = 4         # server -> client : acknowledges reliable msgs


class EventType(IntEnum):
    ACQUIRE_REQUEST = 0
    # add more event types later if needed


class AckType(IntEnum):
    EVENT_ACK = 0
    # extend later if you ACK other things


# Header: 4s B B I I Q H I  => 28 bytes total
HEADER_FORMAT = "!4s B B I I Q H I"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)

# Event payload:
#   uint8  event_type
#   uint32 event_id
#   uint8  row
#   uint8  col
#   uint64 client_ts_ms
EVENT_FORMAT = "!B I B B Q"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)

# ACK payload:
#   uint8  ack_type
#   uint32 event_id
ACK_FORMAT = "!B I"
ACK_SIZE = struct.calcsize(ACK_FORMAT)


# ---- Helpers ----

def current_millis():
    """Return current time in milliseconds since epoch."""
    return int(time.time() * 1000)


def pack_header(
    msg_type,
    snapshot_id,
    seq_num,
    payload_len,
    checksum=0,
    ts_ms=None,
):
    """
    Build a binary header for one protocol packet.
    """
    if ts_ms is None:
        ts_ms = current_millis()

    return struct.pack(
        HEADER_FORMAT,
        PROTOCOL_ID,
        VERSION,
        int(msg_type),
        int(snapshot_id),
        int(seq_num),
        int(ts_ms),
        int(payload_len),
        int(checksum),
    )


def unpack_header(data):
    """
    Parse a binary header and return a dict with its fields.

    Raises ValueError if header is too short or invalid.
    """
    if len(data) < HEADER_SIZE:
        raise ValueError("Data too short for header: %d bytes" % len(data))

    (
        proto_id,
        version,
        msg_type,
        snapshot_id,
        seq_num,
        server_ts_ms,
        payload_len,
        checksum,
    ) = struct.unpack(HEADER_FORMAT, data[:HEADER_SIZE])

    if proto_id != PROTOCOL_ID:
        raise ValueError("Invalid protocol id: %r" % (proto_id,))
    if version != VERSION:
        raise ValueError("Unsupported version: %d" % version)

    return {
        "msg_type": MsgType(msg_type),
        "snapshot_id": snapshot_id,
        "seq_num": seq_num,
        "server_timestamp": server_ts_ms,
        "payload_len": payload_len,
        "checksum": checksum,
    }


# ---- Event helpers ----

def pack_event(event_type, event_id, row, col, client_ts_ms=None):
    """
    Build an EVENT payload.

    event_type: EventType
    event_id: unique per client (uint32)
    row, col: 0..GRID_SIZE-1
    client_ts_ms: optional client timestamp in ms, auto if None
    """
    if client_ts_ms is None:
        client_ts_ms = current_millis()

    return struct.pack(
        EVENT_FORMAT,
        int(event_type),
        int(event_id),
        int(row),
        int(col),
        int(client_ts_ms),
    )


def unpack_event(data):
    """
    Parse an EVENT payload into a dict.

    Raises ValueError if too short.
    """
    if len(data) < EVENT_SIZE:
        raise ValueError("Event payload too short: %d bytes" % len(data))

    event_type, event_id, row, col, client_ts_ms = struct.unpack(
        EVENT_FORMAT, data[:EVENT_SIZE]
    )
    return {
        "event_type": EventType(event_type),
        "event_id": event_id,
        "row": row,
        "col": col,
        "client_ts_ms": client_ts_ms,
    }


# ---- ACK helpers ----

def pack_ack(ack_type, event_id):
    return struct.pack(ACK_FORMAT, int(ack_type), int(event_id))


def unpack_ack(data):
    if len(data) < ACK_SIZE:
        raise ValueError("ACK payload too short: %d bytes" % len(data))
    ack_type, event_id = struct.unpack(ACK_FORMAT, data[:ACK_SIZE])
    return {
        "ack_type": AckType(ack_type),
        "event_id": event_id,
    }


# ---- Manual sanity check ----

if __name__ == "__main__":
    print("HEADER_SIZE =", HEADER_SIZE, "bytes")
    print("EVENT_SIZE  =", EVENT_SIZE, "bytes")
    print("ACK_SIZE    =", ACK_SIZE, "bytes")

    hdr = pack_header(
        msg_type=MsgType.SNAPSHOT,
        snapshot_id=42,
        seq_num=7,
        payload_len=100,
    )
    print("Packed header length =", len(hdr))
    print("Parsed header:", unpack_header(hdr))

    ev = pack_event(EventType.ACQUIRE_REQUEST, event_id=1, row=3, col=5)
    print("Packed event length =", len(ev))
    print("Parsed event:", unpack_event(ev))

    ack = pack_ack(AckType.EVENT_ACK, event_id=1)
    print("Packed ack length =", len(ack))
    print("Parsed ack:", unpack_ack(ack))
