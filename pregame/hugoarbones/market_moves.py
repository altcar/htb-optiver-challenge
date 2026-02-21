"""OptiBook WebSocket client - Trade streaming library

Connects to OptiBook WebSocket and provides a clean stream of trades.

Usage:
    from optibook_client import OptiBookTradeStream
    
    def handle_trade(trade):
        print(f"Trade: {trade['buyer']} bought {trade['volume']} {trade['instrument']} @ {trade['price']}")
    
    stream = OptiBookTradeStream("htb.optibook.net", "team-005")
    stream.on_trade = handle_trade
    stream.start()
    
    # Keep running...
    import time
    while True:
        time.sleep(1)

Dependencies: websocket-client, msgpack, lz4
"""
import time
import threading
import urllib.parse
from typing import Optional, Callable, Any, Dict, List
from collections import defaultdict, deque
from datetime import datetime

import msgpack
import lz4.frame
import websocket


class OptiBookClient:
    """Low-level WebSocket client for OptiBook exchange."""

    def __init__(self, host: str, username: Optional[str] = None):
        self.host = host
        self.username = username
        self.ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.on_message: Optional[Callable[[Any], None]] = None
        self.on_open: Optional[Callable[[], None]] = None
        self.on_close: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        self._connected = False

    def _build_url(self) -> str:
        params = {"username": self.username} if self.username else {}
        qs = urllib.parse.urlencode(params)
        return f"wss://{self.host}/ws?{qs}" if qs else f"wss://{self.host}/ws"

    @staticmethod
    def _pack_and_compress(obj: Any) -> bytes:
        packed = msgpack.packb(obj, use_bin_type=True)
        return lz4.frame.compress(packed)

    @staticmethod
    def _decompress_and_unpack(data: bytes) -> Any:
        decompressed = lz4.frame.decompress(data)
        return msgpack.unpackb(decompressed, raw=False)

    def send_message(self, obj: Any) -> None:
        """Send a message to the server."""
        if not self.ws or not self.ws.sock or not self.ws.sock.connected:
            raise RuntimeError("WebSocket is not connected")
        data = self._pack_and_compress(obj)
        self.ws.send(data, opcode=websocket.ABNF.OPCODE_BINARY)

    def send_select(self, instrument: Optional[str] = None, team: Optional[str] = None) -> None:
        """Send a SELECT message to subscribe to data."""
        msg = {
            "type": "SELECT",
            "team": self.username,
            "data": {"instrument": instrument, "team": team},
        }
        self.send_message(msg)

    def is_connected(self) -> bool:
        """Check if the client is connected."""
        return self._connected

    def _on_open(self, ws):
        self._connected = True
        if self.on_open:
            self.on_open()

    def _on_close(self, ws, close_status_code, close_msg):
        self._connected = False
        if self.on_close:
            self.on_close()

    def _on_error(self, ws, err):
        if self.on_error:
            self.on_error(err)

    def _on_message(self, ws, message):
        try:
            if isinstance(message, (bytes, bytearray)):
                try:
                    obj = self._decompress_and_unpack(bytes(message))
                except Exception:
                    obj = msgpack.unpackb(message, raw=False)
                if self.on_message:
                    self.on_message(obj)
        except Exception as e:
            if self.on_error:
                self.on_error(e)

    def connect(self) -> None:
        """Start connection in background thread with auto-reconnect."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()

        def run():
            backoff = 1.0
            while not self._stop_event.is_set():
                url = self._build_url()
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_close=self._on_close,
                    on_error=self._on_error,
                )
                try:
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception as e:
                    if self.on_error:
                        self.on_error(e)
                
                if self._stop_event.is_set():
                    break
                time.sleep(backoff)
                backoff = min(backoff * 1.5, 30.0)

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()

    def close(self):
        """Close the WebSocket connection."""
        self._stop_event.set()
        self._connected = False
        if self.ws:
            self.ws.close()
        if self._thread:
            self._thread.join(timeout=2.0)


class OptiBookTradeStream:
    """High-level trade streaming interface.
    
    Automatically connects and streams trades. Set the `on_trade` callback
    to receive individual trades as they happen.
    
    Example:
        stream = OptiBookTradeStream("htb.optibook.net", "team-004")
        stream.on_trade = lambda trade: print(trade)
        stream.start()
    """

    def __init__(self, host: str, username: str, auto_subscribe: bool = True):
        """
        Args:
            host: OptiBook server hostname
            username: Your team username
            auto_subscribe: Automatically subscribe to all trades on connect
        """
        self.client = OptiBookClient(host, username)
        self.auto_subscribe = auto_subscribe
        self._running = False
        
        # Callbacks
        self.on_trade: Optional[Callable[[Dict], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        
        # Optional: Store trades
        self.store_trades = False
        self.trades_by_team = defaultdict(lambda: deque(maxlen=1000))
        self.all_trades = deque(maxlen=10000)
        
        # Wire up callbacks
        self.client.on_message = self._handle_message
        self.client.on_open = self._handle_open
        self.client.on_close = self._handle_close
        self.client.on_error = self._handle_error

    def _handle_message(self, obj: Any):
        """Process incoming messages and extract trades."""
        try:
            if not isinstance(obj, dict):
                return
            
            module = obj.get("module")
            msg_type = obj.get("type")
            
            if module == "trade_ticker" and msg_type == "module_data":
                self._process_trades(obj.get("data", {}))
        except Exception as e:
            if self.on_error:
                self.on_error(e)

    def _process_trades(self, data: Dict):
        """Extract and emit individual trades."""
        instruments = data.get("instrumentId", [])
        buyers = data.get("buyer", [])
        sellers = data.get("seller", [])
        volumes = data.get("volume", [])
        prices = data.get("price", [])
        timestamps = data.get("timestamp", [])
        
        for i in range(len(instruments)):
            trade = {
                "timestamp": timestamps[i] if i < len(timestamps) else datetime.utcnow().isoformat(),
                "instrument": instruments[i],
                "price": float(prices[i]) if i < len(prices) else None,
                "volume": float(volumes[i]) if i < len(volumes) else 0.0,
                "buyer": buyers[i] if i < len(buyers) else None,
                "seller": sellers[i] if i < len(sellers) else None,
            }
            
            # Store if enabled
            if self.store_trades:
                self.all_trades.append(trade)
                if trade["buyer"]:
                    self.trades_by_team[trade["buyer"]].append({**trade, "side": "BUY"})
                if trade["seller"]:
                    self.trades_by_team[trade["seller"]].append({**trade, "side": "SELL"})
            
            # Emit trade
            if self.on_trade:
                self.on_trade(trade)

    def _handle_open(self):
        """Handle connection opened."""
        if self.auto_subscribe:
            try:
                self.client.send_select(None, None)
            except Exception as e:
                if self.on_error:
                    self.on_error(e)
        
        if self.on_connected:
            self.on_connected()

    def _handle_close(self):
        """Handle connection closed."""
        if self.on_disconnected:
            self.on_disconnected()

    def _handle_error(self, error: Exception):
        """Handle errors."""
        if self.on_error:
            self.on_error(error)

    def start(self, blocking: bool = False):
        """Start the trade stream.
        
        Args:
            blocking: If True, blocks until stopped. If False, runs in background.
        """
        if self._running:
            return
        
        self._running = True
        self.client.connect()
        
        if blocking:
            try:
                while self._running:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                self.stop()

    def stop(self):
        """Stop the trade stream."""
        self._running = False
        self.client.close()

    def is_connected(self) -> bool:
        """Check if currently connected."""
        return self.client.is_connected()

    def get_team_trades(self, team: str, limit: Optional[int] = None) -> List[Dict]:
        """Get stored trades for a team (only if store_trades=True)."""
        if not self.store_trades:
            raise RuntimeError("Trade storage is disabled. Set store_trades=True")
        trades = list(self.trades_by_team[team])
        if limit:
            return trades[-limit:]
        return trades

    def get_recent_trades(self, limit: Optional[int] = None) -> List[Dict]:
        """Get recent trades across all teams (only if store_trades=True)."""
        if not self.store_trades:
            raise RuntimeError("Trade storage is disabled. Set store_trades=True")
        trades = list(self.all_trades)
        if limit:
            return trades[-limit:]
        return trades


# Example usage
if __name__ == "__main__":
    def handle_trade(trade):
        print(f"[{trade['timestamp']}] {trade['instrument']}: "
              f"{trade['buyer']} -> {trade['seller']} | "
              f"{trade['volume']} @ {trade['price']}")

    def handle_connected():
        print("✓ Connected and streaming trades...")

    def handle_disconnected():
        print("✗ Disconnected, will retry...")

    def handle_error(error):
        print(f"✗ Error: {error}")

    # Create stream
    stream = OptiBookTradeStream("htb.optibook.net", "team-004")
    
    # Set callbacks
    stream.on_trade = handle_trade
    stream.on_connected = handle_connected
    stream.on_disconnected = handle_disconnected
    stream.on_error = handle_error
    
    # Optional: Enable trade storage for later analysis
    stream.store_trades = True
    
    print("Starting trade stream...")
    
    # Start in blocking mode
    stream.start(blocking=True)
