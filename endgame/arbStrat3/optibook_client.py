"""OptiBook WebSocket client - Multi-stream data collection library

Connects to OptiBook WebSocket and captures orderbooks, trades, and news.
Continuously saves all data to a SQLite database with timestamps.

Usage:
    from optibook_client import OptiBookDataLogger
    
    logger = OptiBookDataLogger(
        host="htb.optibook.net",
        username="team-005",
        db_path="market_data.db"
    )
    
    logger.start()
    
    # Keep running...
    import time
    while True:
        time.sleep(1)

Dependencies: websocket-client, msgpack, lz4
"""
import time
import threading
import urllib.parse
import sqlite3
import json
from typing import Optional, Callable, Any, Dict, List
from collections import defaultdict, deque
from datetime import datetime
from queue import Queue

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
    
    def subscribe_orderbook(self, instrument: Optional[str] = None) -> None:
        """Subscribe to orderbook updates."""
        msg = {
            "type": "SUBSCRIBE",
            "module": "book_viewer",
            "data": {"instrument": instrument}
        }
        self.send_message(msg)
    
    def subscribe_news(self) -> None:
        """Subscribe to news updates."""
        msg = {
            "type": "SUBSCRIBE",
            "module": "news_ticker",
            "data": {}
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
        self.on_orderbook: Optional[Callable[[Dict], None]] = None
        self.on_news: Optional[Callable[[Dict], None]] = None
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None
        
        # Optional: Store data
        self.store_trades = False
        self.trades_by_team = defaultdict(lambda: deque(maxlen=1000))
        self.all_trades = deque(maxlen=10000)
        
        # Wire up callbacks
        self.client.on_message = self._handle_message
        self.client.on_open = self._handle_open
        self.client.on_close = self._handle_close
        self.client.on_error = self._handle_error

    def _handle_message(self, obj: Any):
        """Process incoming messages and extract trades, orderbooks, and news."""
        try:
            if not isinstance(obj, dict):
                return
            
            module = obj.get("module")
            msg_type = obj.get("type")
            
            # Handle trades
            if module == "trade_ticker" and msg_type == "module_data":
                self._process_trades(obj.get("data", {}))
            
            # Handle orderbook updates from instruments_overview
            elif module == "instruments_overview" and msg_type == "module_data":
                self._process_orderbook(obj.get("data", {}))
            
            # Handle news from news_feed
            elif module == "news_feed" and msg_type == "module_data":
                self._process_news(obj.get("data", {}))
                
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
                "timestamp": timestamps[i] if i < len(timestamps) else int(datetime.utcnow().timestamp()),
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

    def _process_orderbook(self, data: Dict):
        """Extract and emit orderbook updates from instruments_overview."""
        if not self.on_orderbook:
            return
        
        # instruments_overview format has arrays for each field
        instruments = data.get("instrumentId", [])
        best_bids = data.get("bestBid", [])
        best_asks = data.get("bestAsk", [])
        best_bid_volumes = data.get("bestBidVolume", [])
        best_ask_volumes = data.get("bestAskVolume", [])
        
        # Process each instrument
        for i in range(len(instruments)):
            instrument = instruments[i] if i < len(instruments) else None
            if not instrument:
                continue
            
            best_bid = best_bids[i] if i < len(best_bids) else None
            best_ask = best_asks[i] if i < len(best_asks) else None
            bid_volume = best_bid_volumes[i] if i < len(best_bid_volumes) else 0
            ask_volume = best_ask_volumes[i] if i < len(best_ask_volumes) else 0
            
            # Ensure best_bid and best_ask are scalar values
            if isinstance(best_bid, (list, tuple)):
                best_bid = best_bid[0] if best_bid else None
            if isinstance(best_ask, (list, tuple)):
                best_ask = best_ask[0] if best_ask else None
            if isinstance(bid_volume, (list, tuple)):
                bid_volume = bid_volume[0] if bid_volume else 0
            if isinstance(ask_volume, (list, tuple)):
                ask_volume = ask_volume[0] if ask_volume else 0
            
            # Convert to float if possible
            try:
                best_bid = float(best_bid) if best_bid is not None else None
                best_ask = float(best_ask) if best_ask is not None else None
                bid_volume = float(bid_volume) if bid_volume else 0
                ask_volume = float(ask_volume) if ask_volume else 0
            except (ValueError, TypeError):
                continue  # Skip invalid data
            
            spread = (best_ask - best_bid) if (best_bid and best_ask and best_bid > 0 and best_ask > 0) else None
            
            # Create simplified orderbook with best bid/ask
            # Note: full depth not available in instruments_overview
            bids = [[best_bid, bid_volume]] if best_bid and bid_volume else []
            asks = [[best_ask, ask_volume]] if best_ask and ask_volume else []
            
            orderbook = {
                "timestamp": int(datetime.utcnow().timestamp()),
                "instrument": instrument,
                "bids": bids,
                "asks": asks,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": spread,
            }
            self.on_orderbook(orderbook)

    def _process_news(self, data: Dict):
        """Extract and emit news items from news_feed."""
        if not self.on_news:
            return
        
        # news_feed format has timestamp and post
        timestamp = data.get("timestamp")
        post = data.get("post", [])
        
        # Convert timestamp if it's a list
        if isinstance(timestamp, (list, tuple)):
            timestamp = timestamp[0] if timestamp else None
        
        # Ensure timestamp is an integer
        try:
            timestamp = int(timestamp) if timestamp else int(datetime.utcnow().timestamp())
        except (ValueError, TypeError):
            timestamp = int(datetime.utcnow().timestamp())
        
        # Process each news post
        for news_item in post:
            if isinstance(news_item, dict):
                news = {
                    "timestamp": timestamp,
                    "headline": news_item.get("headline") or news_item.get("title") or "",
                    "body": news_item.get("body") or news_item.get("content") or str(news_item),
                    "instrument": news_item.get("instrumentId") or news_item.get("instrument") or "GENERAL",
                    "source": news_item.get("source") or "OptiBook",
                }
                self.on_news(news)
            elif isinstance(news_item, str):
                # Simple string post
                news = {
                    "timestamp": timestamp,
                    "headline": news_item[:100],  # First 100 chars as headline
                    "body": news_item,
                    "instrument": "GENERAL",
                    "source": "OptiBook",
                }
                self.on_news(news)

    def _handle_open(self):
        """Handle connection opened."""
        if self.auto_subscribe:
            try:
                print("[DEBUG] Subscribing to data streams...")
                # Subscribe to trades
                self.client.send_select(None, None)
                print("[DEBUG] Subscribed to trades")
                
                # Subscribe to orderbooks for all instruments
                self.client.subscribe_orderbook(None)
                print("[DEBUG] Subscribed to orderbooks")
                
                # Subscribe to news
                self.client.subscribe_news()
                print("[DEBUG] Subscribed to news")
            except Exception as e:
                print(f"[DEBUG] Subscription error: {e}")
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


class DatabaseManager:
    """Manages SQLite database for storing market data."""
    
    def __init__(self, db_path: str):
        """Initialize database manager.
        
        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self._connection = None
        self._write_queue = Queue()
        self._writer_thread = None
        self._running = False
        self._init_database()
    
    def _init_database(self):
        """Create database tables if they don't exist."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Trades table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                instrument TEXT NOT NULL,
                price REAL,
                volume REAL NOT NULL,
                buyer TEXT,
                seller TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Orderbooks table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orderbooks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                instrument TEXT NOT NULL,
                bids TEXT,
                asks TEXT,
                best_bid REAL,
                best_ask REAL,
                spread REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # News table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                headline TEXT,
                body TEXT,
                instrument TEXT,
                source TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Create indexes for faster queries
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_instrument ON trades(instrument)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_buyer ON trades(buyer)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_seller ON trades(seller)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orderbooks_timestamp ON orderbooks(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orderbooks_instrument ON orderbooks(instrument)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_news_timestamp ON news(timestamp)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_news_instrument ON news(instrument)")
        
        conn.commit()
        conn.close()
    
    def start(self):
        """Start background writer thread."""
        if self._running:
            return
        
        self._running = True
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self._writer_thread.start()
    
    def stop(self):
        """Stop background writer and flush queue."""
        self._running = False
        if self._writer_thread:
            self._writer_thread.join(timeout=5.0)
    
    def _writer_loop(self):
        """Background thread that writes data to database."""
        conn = sqlite3.connect(self.db_path)
        
        while self._running or not self._write_queue.empty():
            try:
                # Batch process up to 100 items at once
                batch = []
                for _ in range(100):
                    if not self._write_queue.empty():
                        batch.append(self._write_queue.get_nowait())
                    else:
                        break
                
                if not batch:
                    time.sleep(0.1)
                    continue
                
                cursor = conn.cursor()
                for item_type, data in batch:
                    if item_type == "trade":
                        cursor.execute("""
                            INSERT INTO trades (timestamp, instrument, price, volume, buyer, seller)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (
                            data["timestamp"],
                            data["instrument"],
                            data["price"],
                            data["volume"],
                            data["buyer"],
                            data["seller"]
                        ))
                    
                    elif item_type == "orderbook":
                        # Debug: check data types
                        try:
                            cursor.execute("""
                                INSERT INTO orderbooks (timestamp, instrument, bids, asks, best_bid, best_ask, spread)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            """, (
                                data["timestamp"],
                                data["instrument"],
                                json.dumps(data["bids"]),
                                json.dumps(data["asks"]),
                                data["best_bid"],
                                data["best_ask"],
                                data["spread"]
                            ))
                        except Exception as e:
                            print(f"[DEBUG] Orderbook insert error: {e}")
                            print(f"[DEBUG] Data types: timestamp={type(data['timestamp'])}, instrument={type(data['instrument'])}, best_bid={type(data['best_bid'])}, best_ask={type(data['best_ask'])}, spread={type(data['spread'])}")
                            print(f"[DEBUG] Values: {data}")
                            raise
                    
                    elif item_type == "news":
                        cursor.execute("""
                            INSERT INTO news (timestamp, headline, body, instrument, source)
                            VALUES (?, ?, ?, ?, ?)
                        """, (
                            data["timestamp"],
                            data["headline"],
                            data["body"],
                            data["instrument"],
                            data["source"]
                        ))
                
                conn.commit()
                
            except Exception as e:
                print(f"Database error: {e}")
                time.sleep(1.0)
        
        conn.close()
    
    def save_trade(self, trade: Dict):
        """Queue a trade for saving."""
        self._write_queue.put(("trade", trade))
    
    def save_orderbook(self, orderbook: Dict):
        """Queue an orderbook for saving."""
        self._write_queue.put(("orderbook", orderbook))
    
    def save_news(self, news: Dict):
        """Queue a news item for saving."""
        self._write_queue.put(("news", news))
    
    def query_trades(self, instrument: Optional[str] = None, 
                    limit: int = 100) -> List[Dict]:
        """Query trades from database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if instrument:
            cursor.execute("""
                SELECT timestamp, instrument, price, volume, buyer, seller
                FROM trades
                WHERE instrument = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """, (instrument, limit))
        else:
            cursor.execute("""
                SELECT timestamp, instrument, price, volume, buyer, seller
                FROM trades
                ORDER BY timestamp DESC
                LIMIT ?
            """, (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [
            {
                "timestamp": row[0],
                "instrument": row[1],
                "price": row[2],
                "volume": row[3],
                "buyer": row[4],
                "seller": row[5]
            }
            for row in rows
        ]
    
    def get_stats(self) -> Dict:
        """Get database statistics."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute("SELECT COUNT(*) FROM trades")
        trade_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM orderbooks")
        orderbook_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM news")
        news_count = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            "trades": trade_count,
            "orderbooks": orderbook_count,
            "news": news_count
        }


class OptiBookDataLogger:
    """High-level data logger that captures trades, orderbooks, and news to database.
    
    Automatically connects and streams all data types, saving them continuously
    to a SQLite database.
    
    Example:
        logger = OptiBookDataLogger("htb.optibook.net", "team-004", "market_data.db")
        logger.start(blocking=True)
    """
    
    def __init__(self, host: str, username: str, db_path: str = "market_data.db"):
        """
        Args:
            host: OptiBook server hostname
            username: Your team username
            db_path: Path to SQLite database file
        """
        self.stream = OptiBookTradeStream(host, username)
        self.db = DatabaseManager(db_path)
        
        # Statistics
        self.stats = {
            "trades": 0,
            "orderbooks": 0,
            "news": 0,
            "connected": False
        }
        
        # Wire up callbacks
        self.stream.on_trade = self._handle_trade
        self.stream.on_orderbook = self._handle_orderbook
        self.stream.on_news = self._handle_news
        self.stream.on_connected = self._handle_connected
        self.stream.on_disconnected = self._handle_disconnected
        self.stream.on_error = self._handle_error
    
    def _handle_trade(self, trade: Dict):
        """Save trade to database."""
        self.db.save_trade(trade)
        self.stats["trades"] += 1
        print(f"[TRADE] {trade['instrument']}: {trade['buyer']} -> {trade['seller']} | "
              f"{trade['volume']} @ {trade['price']}")
    
    def _handle_orderbook(self, orderbook: Dict):
        """Save orderbook to database."""
        self.db.save_orderbook(orderbook)
        self.stats["orderbooks"] += 1
        if self.stats["orderbooks"] % 100 == 0:  # Print every 100th orderbook
            print(f"[ORDERBOOK] {orderbook['instrument']}: "
                  f"Bid={orderbook['best_bid']} Ask={orderbook['best_ask']} "
                  f"Spread={orderbook['spread']}")
    
    def _handle_news(self, news: Dict):
        """Save news to database."""
        self.db.save_news(news)
        self.stats["news"] += 1
        print(f"[NEWS] {news['headline']}")
    
    def _handle_connected(self):
        """Handle connection."""
        self.stats["connected"] = True
        print("✓ Connected to OptiBook - Logging data to database...")
    
    def _handle_disconnected(self):
        """Handle disconnection."""
        self.stats["connected"] = False
        print("✗ Disconnected - Will retry...")
    
    def _handle_error(self, error: Exception):
        """Handle errors."""
        print(f"✗ Error: {error}")
    
    def start(self, blocking: bool = False):
        """Start logging data.
        
        Args:
            blocking: If True, blocks until stopped. If False, runs in background.
        """
        self.db.start()
        self.stream.start(blocking=blocking)
    
    def stop(self):
        """Stop logging and close database."""
        self.stream.stop()
        self.db.stop()
    
    def get_stats(self) -> Dict:
        """Get current statistics."""
        db_stats = self.db.get_stats()
        return {
            **self.stats,
            "db_stats": db_stats
        }


# Example usage
if __name__ == "__main__":
    # Simple example using the data logger
    logger = OptiBookDataLogger("htb.optibook.net", "team-004", "market_data.db")
    
    print("Starting data logger...")
    print("Capturing trades, orderbooks, and news to database...")
    
    try:
        # Start in blocking mode
        logger.start(blocking=True)
    except KeyboardInterrupt:
        print("\nStopping logger...")
        logger.stop()
        stats = logger.get_stats()
        print(f"\nFinal Statistics:")
        print(f"  Trades captured: {stats['trades']}")
        print(f"  Orderbooks captured: {stats['orderbooks']}")
        print(f"  News items captured: {stats['news']}")
        print(f"  Database stats: {stats['db_stats']}")

