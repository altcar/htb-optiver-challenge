import time
import threading
import urllib.parse
from typing import Optional, Callable, Any, Dict, List
from collections import defaultdict, deque
from datetime import datetime

import msgpack
import lz4.frame
import websocket


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
