# game_ui.py
"""
Grid Clash - Pygame Graphical Client

A 2D graphical interface for the Grid Clash multiplayer game.
Uses the GCSP (Grid Clash Sync Protocol) over UDP.

Features:
- 20x20 grid visualization with player-colored cells
- Mouse click to acquire cells
- Real-time synchronization with server snapshots
- Status panel showing connection info and pending events
"""

import pygame
import socket
import struct
import threading
import time
import os
from collections import deque

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

# ============ Configuration ============
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 50000

GRID_SIZE = 20
CELL_SIZE = 30
GRID_PADDING = 20
STATUS_HEIGHT = 100

WINDOW_WIDTH = GRID_SIZE * CELL_SIZE + GRID_PADDING * 2
WINDOW_HEIGHT = GRID_SIZE * CELL_SIZE + GRID_PADDING * 2 + STATUS_HEIGHT

FPS = 60
RETRY_INTERVAL_MS = 200
MAX_RETRIES = 3

# ============ Colors ============
COLOR_BG = (30, 30, 40)
COLOR_GRID_LINE = (60, 60, 80)
COLOR_UNCLAIMED = (80, 80, 100)
COLOR_HOVER = (120, 120, 150)
COLOR_PENDING = (200, 200, 100)
COLOR_TEXT = (220, 220, 230)
COLOR_STATUS_BG = (40, 40, 55)

# Player colors (up to 8 players)
PLAYER_COLORS = [
    (46, 204, 113),   # Green - Player 1
    (52, 152, 219),   # Blue - Player 2
    (231, 76, 60),    # Red - Player 3
    (155, 89, 182),   # Purple - Player 4
    (241, 196, 15),   # Yellow - Player 5
    (26, 188, 156),   # Teal - Player 6
    (230, 126, 34),   # Orange - Player 7
    (149, 165, 166),  # Silver - Player 8
]

def get_player_color(player_id):
    """Get color for a player ID (1-indexed)."""
    if player_id <= 0:
        return COLOR_UNCLAIMED
    idx = (player_id - 1) % len(PLAYER_COLORS)
    return PLAYER_COLORS[idx]


# ============ Game State ============
class GameState:
    """Thread-safe game state shared between UI and network threads."""
    
    def __init__(self):
        self.lock = threading.Lock()
        
        # Connection state
        self.player_id = None
        self.connected = False
        self.last_snapshot_id = -1
        self.last_latency_ms = 0
        
        # Grid state: list of owner IDs (0 = unclaimed)
        self.grid = [0] * (GRID_SIZE * GRID_SIZE)
        
        # Pending events: event_id -> {row, col, last_send_ms, retries}
        self.pending_events = {}
        self.next_event_id = 1
        
        # For jitter calculation
        self.last_snapshot_latency_ms = None
        self.jitter_ms = 0
    
    def get_cell(self, row, col):
        """Get the owner of a cell."""
        with self.lock:
            idx = row * GRID_SIZE + col
            return self.grid[idx]
    
    def is_pending(self, row, col):
        """Check if a cell has a pending acquire request."""
        with self.lock:
            for ev in self.pending_events.values():
                if ev["row"] == row and ev["col"] == col:
                    return True
            return False
    
    def update_from_snapshot(self, snapshot_id, grid_data, latency_ms):
        """Update state from a snapshot payload."""
        with self.lock:
            if snapshot_id <= self.last_snapshot_id:
                return False  # Outdated snapshot
            
            self.last_snapshot_id = snapshot_id
            self.connected = True
            self.last_latency_ms = latency_ms
            
            # Jitter
            if self.last_snapshot_latency_ms is not None:
                self.jitter_ms = abs(latency_ms - self.last_snapshot_latency_ms)
            self.last_snapshot_latency_ms = latency_ms
            
            # Update grid
            for i, owner in enumerate(grid_data):
                if i < len(self.grid):
                    self.grid[i] = owner
            
            return True
    
    def add_pending_event(self, event_id, row, col):
        """Add a new pending event."""
        with self.lock:
            self.pending_events[event_id] = {
                "row": row,
                "col": col,
                "last_send_ms": current_millis(),
                "retries": 0,
            }
    
    def ack_event(self, event_id):
        """Remove an event when ACK is received."""
        with self.lock:
            if event_id in self.pending_events:
                del self.pending_events[event_id]
                return True
            return False
    
    def get_pending_count(self):
        """Get number of pending events."""
        with self.lock:
            return len(self.pending_events)


# ============ Network Thread ============
class NetworkThread(threading.Thread):
    """Background thread for network communication."""
    
    def __init__(self, state: GameState):
        super().__init__(daemon=True)
        self.state = state
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(0.1)  # Non-blocking with timeout
        self.running = True
        self.server_addr = (SERVER_HOST, SERVER_PORT)
        
        # Event queue for sending
        self.send_queue = deque()
        self.send_lock = threading.Lock()
    
    def send_join(self):
        """Send JOIN request to server."""
        hdr = pack_header(
            msg_type=MsgType.JOIN,
            snapshot_id=0,
            seq_num=0,
            payload_len=0,
        )
        self.sock.sendto(hdr, self.server_addr)
        print("[UI] JOIN sent")
    
    def send_acquire(self, row, col):
        """Queue an acquire request."""
        with self.send_lock:
            with self.state.lock:
                ev_id = self.state.next_event_id
                self.state.next_event_id += 1
            
            self.send_queue.append((ev_id, row, col))
    
    def _send_event(self, ev_id, row, col):
        """Actually send an event packet."""
        payload = pack_event(EventType.ACQUIRE_REQUEST, ev_id, row, col)
        hdr = pack_header(
            msg_type=MsgType.EVENT,
            snapshot_id=0,
            seq_num=0,
            payload_len=len(payload),
        )
        self.sock.sendto(hdr + payload, self.server_addr)
        self.state.add_pending_event(ev_id, row, col)
        print(f"[UI] EVENT {ev_id} sent -> ({row}, {col})")
    
    def _process_send_queue(self):
        """Process queued events."""
        with self.send_lock:
            while self.send_queue:
                ev_id, row, col = self.send_queue.popleft()
                self._send_event(ev_id, row, col)
    
    def _handle_retries(self):
        """Resend pending events if needed."""
        now = current_millis()
        
        with self.state.lock:
            to_remove = []
            for ev_id, info in list(self.state.pending_events.items()):
                if now - info["last_send_ms"] < RETRY_INTERVAL_MS:
                    continue
                
                if info["retries"] >= MAX_RETRIES:
                    print(f"[UI] EVENT {ev_id} failed after {MAX_RETRIES} retries")
                    to_remove.append(ev_id)
                    continue
                
                # Resend
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
                self.sock.sendto(hdr + payload, self.server_addr)
                info["last_send_ms"] = now
                info["retries"] += 1
                print(f"[UI] Resent EVENT {ev_id} (retry {info['retries']})")
            
            for ev_id in to_remove:
                del self.state.pending_events[ev_id]
    
    def _decode_snapshot(self, payload):
        """Decode snapshot payload and return grid data."""
        if len(payload) < 2:
            return None
        
        offset = 0
        N, num_players = struct.unpack_from("!B B", payload, offset)
        offset += 2
        
        # Skip player positions
        player_struct_size = struct.calcsize("!B f f")
        offset += num_players * player_struct_size
        
        # Read grid cells
        grid_data = []
        for i in range(N * N):
            if offset < len(payload):
                owner = struct.unpack_from("!B", payload, offset)[0]
                grid_data.append(owner)
                offset += 1
            else:
                grid_data.append(0)
        
        return grid_data
    
    def run(self):
        """Main network loop."""
        self.send_join()
        last_retry_check = current_millis()
        
        while self.running:
            # Process send queue
            self._process_send_queue()
            
            # Check retries periodically
            now = current_millis()
            if now - last_retry_check > 50:
                self._handle_retries()
                last_retry_check = now
            
            # Receive packets
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            
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
                    player_id = struct.unpack("!B", payload)[0]
                    with self.state.lock:
                        self.state.player_id = player_id
                        self.state.connected = True
                    print(f"[UI] JOIN_ACK -> player_id={player_id}")
            
            elif msg_type == MsgType.SNAPSHOT:
                snapshot_id = header["snapshot_id"]
                server_ts = header["server_timestamp"]
                client_rx = current_millis()
                latency = max(0, client_rx - server_ts)
                
                grid_data = self._decode_snapshot(payload)
                if grid_data:
                    self.state.update_from_snapshot(snapshot_id, grid_data, latency)
            
            elif msg_type == MsgType.ACK:
                try:
                    ack = unpack_ack(payload)
                    if ack["ack_type"] == AckType.EVENT_ACK:
                        ev_id = ack["event_id"]
                        if self.state.ack_event(ev_id):
                            print(f"[UI] EVENT_ACK for {ev_id}")
                except ValueError:
                    pass
    
    def stop(self):
        """Stop the network thread."""
        self.running = False
        self.sock.close()


# ============ UI Renderer ============
class GameRenderer:
    """Handles all Pygame rendering."""
    
    def __init__(self, screen, state: GameState):
        self.screen = screen
        self.state = state
        self.font = pygame.font.Font(None, 24)
        self.font_large = pygame.font.Font(None, 32)
        self.pulse_phase = 0
    
    def draw_grid(self, hover_cell=None):
        """Draw the game grid."""
        for row in range(GRID_SIZE):
            for col in range(GRID_SIZE):
                x = GRID_PADDING + col * CELL_SIZE
                y = GRID_PADDING + row * CELL_SIZE
                
                # Determine cell color
                owner = self.state.get_cell(row, col)
                is_pending = self.state.is_pending(row, col)
                is_hover = hover_cell == (row, col)
                
                if is_pending:
                    # Pulsing effect for pending cells
                    pulse = abs(int(50 * (1 + pygame.math.Vector2(1, 0).rotate(self.pulse_phase * 5).x)))
                    color = (200 + pulse % 55, 200 + pulse % 55, 100)
                elif owner > 0:
                    color = get_player_color(owner)
                elif is_hover:
                    color = COLOR_HOVER
                else:
                    color = COLOR_UNCLAIMED
                
                # Draw cell
                rect = pygame.Rect(x + 1, y + 1, CELL_SIZE - 2, CELL_SIZE - 2)
                pygame.draw.rect(self.screen, color, rect, border_radius=3)
                
                # Draw grid line
                pygame.draw.rect(self.screen, COLOR_GRID_LINE, 
                               pygame.Rect(x, y, CELL_SIZE, CELL_SIZE), 1)
        
        self.pulse_phase = (self.pulse_phase + 1) % 360
    
    def draw_status(self):
        """Draw the status panel."""
        status_y = WINDOW_HEIGHT - STATUS_HEIGHT
        
        # Background
        pygame.draw.rect(self.screen, COLOR_STATUS_BG,
                        pygame.Rect(0, status_y, WINDOW_WIDTH, STATUS_HEIGHT))
        pygame.draw.line(self.screen, COLOR_GRID_LINE,
                        (0, status_y), (WINDOW_WIDTH, status_y), 2)
        
        with self.state.lock:
            player_id = self.state.player_id
            connected = self.state.connected
            snapshot_id = self.state.last_snapshot_id
            latency = self.state.last_latency_ms
            jitter = self.state.jitter_ms
            pending = len(self.state.pending_events)
        
        # Title
        title = self.font_large.render("GRID CLASH", True, COLOR_TEXT)
        self.screen.blit(title, (GRID_PADDING, status_y + 10))
        
        # Connection status
        if connected:
            status_text = f"Connected | Player {player_id}"
            status_color = (100, 255, 100)
        else:
            status_text = "Connecting..."
            status_color = (255, 200, 100)
        
        status = self.font.render(status_text, True, status_color)
        self.screen.blit(status, (GRID_PADDING, status_y + 40))
        
        # Stats
        stats_text = f"Snapshot: {snapshot_id} | Latency: {latency}ms | Jitter: {jitter}ms | Pending: {pending}"
        stats = self.font.render(stats_text, True, COLOR_TEXT)
        self.screen.blit(stats, (GRID_PADDING, status_y + 65))
        
        # Player color indicator
        if player_id:
            color = get_player_color(player_id)
            pygame.draw.rect(self.screen, color,
                           pygame.Rect(WINDOW_WIDTH - 60, status_y + 15, 40, 40),
                           border_radius=5)
    
    def draw(self, hover_cell=None):
        """Draw entire frame."""
        self.screen.fill(COLOR_BG)
        self.draw_grid(hover_cell)
        self.draw_status()


# ============ Main Game Loop ============
def get_cell_at_pos(x, y):
    """Get grid cell (row, col) from screen position, or None if outside grid."""
    grid_x = x - GRID_PADDING
    grid_y = y - GRID_PADDING
    
    if grid_x < 0 or grid_y < 0:
        return None
    
    col = grid_x // CELL_SIZE
    row = grid_y // CELL_SIZE
    
    if row >= GRID_SIZE or col >= GRID_SIZE:
        return None
    
    return (row, col)


def main():
    pygame.init()
    
    screen = pygame.display.set_mode((WINDOW_WIDTH, WINDOW_HEIGHT))
    pygame.display.set_caption("Grid Clash - Multiplayer")
    clock = pygame.time.Clock()
    
    # Initialize state and network
    state = GameState()
    network = NetworkThread(state)
    network.start()
    
    # Initialize renderer
    renderer = GameRenderer(screen, state)
    
    running = True
    while running:
        # Handle events
        hover_cell = None
        mouse_x, mouse_y = pygame.mouse.get_pos()
        hover_cell = get_cell_at_pos(mouse_x, mouse_y)
        
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            
            elif event.type == pygame.MOUSEBUTTONDOWN:
                if event.button == 1:  # Left click
                    cell = get_cell_at_pos(event.pos[0], event.pos[1])
                    if cell:
                        row, col = cell
                        # Only send if unclaimed and not pending
                        owner = state.get_cell(row, col)
                        if owner == 0 and not state.is_pending(row, col):
                            network.send_acquire(row, col)
            
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_r:
                    # Rejoin (for testing)
                    network.send_join()
        
        # Render
        renderer.draw(hover_cell)
        pygame.display.flip()
        clock.tick(FPS)
    
    # Cleanup
    network.stop()
    pygame.quit()


if __name__ == "__main__":
    main()
