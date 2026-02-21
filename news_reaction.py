"""
Raw market + news collector

What it does:
- polls order books for all instruments and stores snapshots
- polls social/news feed and stores raw posts
- (optional) listens to trades and stores them too

No guessing of instruments. No derived features. Just raw.

You can later join on time.
"""


import time
import json
import argparse
import datetime as dt
import csv
from collections import defaultdict, deque
from typing import Dict, Any, List
import threading
from pathlib import Path

from clas_trade import OptiBookTradeStream



class RawMarketCollector:
    def __init__(
        self,
        exchange,
        instruments: Dict,
        ob_buffer_len: int = 200_000,
        trade_buffer_len: int = 50_000,
        news_buffer_len: int = 50_000,
    ):
        self.exchange = exchange
        self.instruments = instruments

        # rolling buffers in memory
        self.orderbooks = defaultdict(lambda: deque(maxlen=ob_buffer_len))
        self.trades = deque(maxlen=trade_buffer_len)
        self.news = deque(maxlen=news_buffer_len)

        self.lock = threading.Lock()

        # stats
        self.ob_count = 0
        self.trade_count = 0
        self.news_count = 0

    # 1) order books
    def capture_orderbooks(self):
        now = time.time()
        for inst in self.instruments.values():
            try:
                ob = self.exchange.get_last_price_book(inst.instrument_id)
                if not (ob and ob.bids and ob.asks):
                    continue

                best_bid = ob.bids[0]
                best_ask = ob.asks[0]

                snap = {
                    "ts": now,
                    "instrument_id": inst.instrument_id,
                    "best_bid_price": best_bid.price,
                    "best_bid_vol": best_bid.volume,
                    "best_ask_price": best_ask.price,
                    "best_ask_vol": best_ask.volume,
                    # keep the raw levels too if you want
                    "bids": [(b.price, b.volume) for b in ob.bids[:5]],
                    "asks": [(a.price, a.volume) for a in ob.asks[:5]],
                }

                with self.lock:
                    self.orderbooks[inst.instrument_id].append(snap)
                    self.ob_count += 1
            except Exception:
                pass

    # 2) trades (if you have a trade stream, call this from the callback)
    def handle_trade(self, trade: Dict[str, Any]):
        # trade is used as is, do not interpret
        rec = {
            "ts": time.time(),
            **trade,
        }
        with self.lock:
            self.trades.append(rec)
            self.trade_count += 1

    # 3) news/social
    def handle_news(self, feed_obj: Any):
        """
        feed_obj is whatever exchange.poll_new_social_media_feeds() gives you.
        We just normalise timestamp and text and store the whole thing.
        """
        ts = feed_obj.timestamp
        if isinstance(ts, dt.datetime):
            ts = ts.timestamp()
        rec = {
            "ts": float(ts),
            "raw_text": feed_obj.post,
            # keep original in case there are more fields
            "raw": {
                "timestamp": str(feed_obj.timestamp),
                "post": feed_obj.post,
            },
        }
        with self.lock:
            self.news.append(rec)
            self.news_count += 1

    # 4) save to files
    def save_all(self, prefix: str = "raw_market"):
        with self.lock:
            # orderbooks: we have them per instrument, flatten
            all_obs: List[Dict[str, Any]] = []
            for inst_id, dq in self.orderbooks.items():
                all_obs.extend(list(dq))

            all_trades = list(self.trades)
            all_news = list(self.news)

        with open(f"{prefix}_orderbooks.json", "w") as f:
            json.dump(all_obs, f)
        with open(f"{prefix}_trades.json", "w") as f:
            json.dump(all_trades, f)
        with open(f"{prefix}_news.json", "w") as f:
            json.dump(all_news, f)

        print(f"saved {len(all_obs)} orderbooks, {len(all_trades)} trades, {len(all_news)} news")

    def print_stats(self):
        with self.lock:
            print(
                f"OB: {self.ob_count} | Trades: {self.trade_count} | News: {self.news_count}"
            )


class DataCollector:
    def __init__(self, checkpoint_interval: int = 60):
        self.trades = deque(maxlen=100000)
        self.news = deque(maxlen=50000)
        self.last_checkpoint = time.time()
        self.checkpoint_interval = checkpoint_interval
        self.checkpoint_counter = 0
        self.lock = threading.Lock()
        
        # Create data directory if it doesn't exist
        Path("data").mkdir(exist_ok=True)
        
    def handle_trade(self, trade):
        with self.lock:
            # Normalize timestamp to epoch float (seconds)
            ts = self._to_epoch(trade.get("timestamp", time.time()))
            trade_record = {
                "ts": ts,
                "instrument": trade.get("instrument"),
                "price": trade.get("price"),
                "volume": trade.get("volume"),
                "buyer": trade.get("buyer"),
                "seller": trade.get("seller")
            }
            self.trades.append(trade_record)
            print(f"[TRADE] {trade_record['ts']}: {trade_record['instrument']} - {trade_record['volume']}@{trade_record['price']}")
        self.check_checkpoint()
    
    def handle_news(self, news):
        with self.lock:
            # Normalize timestamp to epoch float (seconds)
            ts = self._to_epoch(news.get("timestamp", time.time()))
            news_record = {
                "ts": ts,
                "post": news.get("post", ""),
                "author": news.get("author", "unknown")
            }
            self.news.append(news_record)
            print(f"[NEWS] {news_record['ts']}: {news_record['author']} - {news_record['post']}")
        self.check_checkpoint()
    
    def save_checkpoint(self):
        with self.lock:
            timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Save trades
            if self.trades:
                # Sort by timestamp before saving
                trades_list = sorted(list(self.trades), key=lambda r: r.get("ts", 0))
                filename = f"data/trades_checkpoint_{timestamp}.csv"
                with open(filename, 'w', newline='') as f:
                    fieldnames = ["ts", "instrument", "price", "volume", "buyer", "seller"]
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(trades_list)
                # also save JSON snapshot for convenience
                with open(f"data/trades_checkpoint_{timestamp}.json", 'w') as jf:
                    json.dump(trades_list, jf)
            
            # Save news
            if self.news:
                # Sort by timestamp before saving
                news_list = sorted(list(self.news), key=lambda r: r.get("ts", 0))
                filename = f"data/news_checkpoint_{timestamp}.csv"
                with open(filename, 'w', newline='') as f:
                    fieldnames = ["ts", "author", "post"]
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    # Ensure order of keys matches fieldnames
                    for n in news_list:
                        writer.writerow({"ts": n.get("ts"), "author": n.get("author"), "post": n.get("post")})
                with open(f"data/news_checkpoint_{timestamp}.json", 'w') as jf:
                    json.dump(news_list, jf)
            
            self.checkpoint_counter += 1
            print(f"\nCheckpoint {self.checkpoint_counter} saved - Trades: {len(self.trades)}, News: {len(self.news)}")

    def _to_epoch(self, ts_val):
        """Convert several timestamp formats to epoch float seconds.

        Accepts float/int (seconds), ISO-8601 strings, datetime objects.
        Falls back to current time on parse failure.
        """
        if ts_val is None:
            return time.time()
        if isinstance(ts_val, (int, float)):
            return float(ts_val)
        if isinstance(ts_val, dt.datetime):
            return float(ts_val.timestamp())
        # string
        try:
            # ISO format
            return float(dt.datetime.fromisoformat(str(ts_val)).timestamp())
        except Exception:
            try:
                return float(str(ts_val))
            except Exception:
                return time.time()
    
    def check_checkpoint(self):
        current_time = time.time()
        if current_time - self.last_checkpoint >= self.checkpoint_interval:
            self.save_checkpoint()
            self.last_checkpoint = current_time

def debug_news(news):
    print(f"[DEBUG] News callback received: {news}")
    
def main():
    # Create data collector
    collector = DataCollector(checkpoint_interval=60)  # Save every 60 seconds
    
    # Create the trade stream
    stream = OptiBookTradeStream("htb.optibook.net", "hugoarbones")
    
    # Enable data storage
    stream.store_trades = True
    stream.news_handler.store_news = True
    
    # Set up callbacks
    stream.on_trade = collector.handle_trade
    stream.on_news = debug_news  # First debug what news we're receiving
    stream.news_handler.on_news = collector.handle_news  # Also set the news handler callback
    
    def on_connected():
        print("[+] Trade and News stream connected")
        # Subscribe to both trade and news data
        stream.client.send_select(subscribe_news=True)
        
    stream.on_connected = on_connected
    stream.on_disconnected = lambda: print("[-] Disconnected, will retry...")
    stream.on_error = lambda e: print(f"[!] Error: {e}")
    
    # Start the stream
    print("Starting data collection...")
    print("Collecting both trades and news...")
    try:
        stream.start(blocking=True)
    except KeyboardInterrupt:
        print("\nStopping...")
        # Save final checkpoint
        collector.save_checkpoint()



if __name__ == "__main__":
    main()
