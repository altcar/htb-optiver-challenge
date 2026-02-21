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
from collections import defaultdict, deque
from typing import Dict, Any, List
import threading

from optibook.synchronous_client import Exchange
from socket_class import OptiBookTradeStream



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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true")
    parser.add_argument("--duration", type=int, default=900)
    parser.add_argument("--prefix", default="raw_market")
    args = parser.parse_args()

    if not args.collect:
        parser.error("use --collect")

    exchange = Exchange()
    exchange.connect()
    instruments = exchange.get_instruments()
    print(f"connected, got {len(instruments)} instruments")

    collector = RawMarketCollector(exchange, instruments)

    start = time.time()
    last_stats = start

    print("collecting raw data... ctrl+c to stop")
    try:
        while time.time() - start < args.duration:
            # always capture the book
            collector.capture_orderbooks()

            # poll your real feed
            social_feeds = exchange.poll_new_social_media_feeds()
            if social_feeds:
                for feed in social_feeds:
                    collector.handle_news(feed)

            stream = OptiBookTradeStream("htb.optibook.net", "hugoarbones")
            stream.on_trade = collector.handle_trade

            time.sleep(0.25)

            if time.time() - last_stats >= 10:
                collector.print_stats()
                last_stats = time.time()

    except KeyboardInterrupt:
        print("stopping...")

    collector.save_all(args.prefix)


if __name__ == "__main__":
    main()
