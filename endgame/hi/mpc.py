import datetime as dt
import time
import logging
import re
import numpy as np
from collections import defaultdict, deque
from math import floor, ceil

from optibook.synchronous_client import Exchange
from optibook.common_types import SocialMediaFeed

logging.getLogger("client").setLevel("ERROR")

# Sentiment keywords
STOCK_KEYWORDS = {'NVDA': ['nvidia', 'nvda'], 'ING': ['ing'], 'SAN': ['santander', 'san'], 'PFE': ['pfizer', 'pfe'], 'CSCO': ['cisco', 'csco']}
POS_WORDS = ['strong', 'boost', 'upgrade', 'growth', 'positive', 'gain', 'bullish', 'buy']
NEG_WORDS = ['weak', 'decline', 'downgrade', 'loss', 'negative', 'drop', 'bearish', 'sell']

def simple_sentiment(post, stock_id):
    post_lower = post.lower()
    if not any(kw in post_lower for kw in STOCK_KEYWORDS.get(stock_id, [])):
        return 0.0
    words = post_lower.split()
    pos = sum(1 for w in words if w in POS_WORDS)
    neg = sum(1 for w in words if w in NEG_WORDS)
    net = (pos - neg) / max(len(words), 1)
    return np.tanh(net * 10)  # -1 to 1, more aggressive scaling

def get_agg_sentiment(history, sid, window=3):  # Shorter window for faster response
    stock_base = sid.split('_')[0]
    scores = [simple_sentiment(f['post'], stock_base) for f in list(history)[-window:]]
    return np.mean([s for s in scores if abs(s) > 0.1]) if any(abs(s) > 0.1 for s in scores) else 0.0

# PID Controller for position unwinding - more aggressive gains
class PIDController:
    def __init__(self, kp=0.0005, ki=0.001, kd=0.02, dt=1.0, max_integral=20.0):  # Adjusted kp for reasonable skew
        self.kp, self.ki, self.kd = kp, ki, kd
        self.dt = dt
        self.max_integral = max_integral
        self.integral = 0.0
        self.prev_error = 0.0

    def update(self, error):
        # Proportional
        p = self.kp * error
        # Integral (clamped)
        self.integral += error * self.dt
        self.integral = np.clip(self.integral, -self.max_integral, self.max_integral)
        i = self.ki * self.integral
        # Derivative
        d = self.kd * (error - self.prev_error) / self.dt
        self.prev_error = error
        return -(p + i + d)  # Negative sign for correct retreat direction

def print_positions_and_pnl(exchange):
    positions = exchange.get_positions()
    pnl = exchange.get_pnl()
    print("Positions:", {k: f"{v:.0f}" for k, v in positions.items()})
    print(f"PnL: {pnl:.2f}")

def round_down_to_tick(price, tick_size): 
    price = max(0.0, min(price, 1000000.0))
    return floor(price / tick_size) * tick_size

def round_up_to_tick(price, tick_size): 
    price = max(0.0, min(price, 1000000.0))
    return ceil(price / tick_size) * tick_size

def clamp_price(price):
    return max(0.0, min(price, 1000000.0))

def insert_quotes(exchange, instrument, bid_price, ask_price, bid_volume, ask_volume):
    bid_price = clamp_price(bid_price)
    ask_price = clamp_price(ask_price)
    if bid_volume > 0:
        exchange.insert_order(
            instrument_id=instrument.instrument_id,
            price=bid_price,
            volume=bid_volume,
            side="bid",
            order_type="limit",
        )
        time.sleep(0.01)  # Faster insertion
    if ask_volume > 0:
        exchange.insert_order(
            instrument_id=instrument.instrument_id,
            price=ask_price,
            volume=ask_volume,
            side="ask",
            order_type="limit",
        )
        time.sleep(0.01)  # Faster insertion

# Main
exchange = Exchange()
exchange.connect()
INSTRUMENTS = exchange.get_instruments()
pid_controllers = {sid: PIDController() for sid in INSTRUMENTS}  # Per-stock PID
SENTIMENT_HISTORY = defaultdict(deque)
sentiment_states = {inst: {'wait_until': 0} for inst in INSTRUMENTS}
STABILIZATION_WAIT = 10  # Shorter wait for faster recovery
BASE_CREDIT = 0.08  # Tighter base spread for more trades
POSITION_LIMIT = 100  # Match exchange limit +/-100
QUOTED_VOLUME = 20  # Larger base volume for aggression

print("Aggressive PID Quoter: Faster, bolder position management for higher PnL.")

while True:
    print(f"\n--- LOOP AT {dt.datetime.now()} ---")
    print_positions_and_pnl(exchange)

    new_feeds = exchange.poll_new_social_media_feeds()
    now = time.time()
    for feed in new_feeds:
        stored = {'timestamp': feed.timestamp, 'post': feed.post}
        for sid in INSTRUMENTS:
            SENTIMENT_HISTORY[sid].append(stored)
            if len(SENTIMENT_HISTORY[sid]) > 10: SENTIMENT_HISTORY[sid].popleft()
        if new_feeds: print(f"Feed: {feed.post[:50]}...")

    for instrument in INSTRUMENTS.values():
        sid = instrument.instrument_id
        exchange.delete_orders(sid)

        book = exchange.get_last_price_book(sid)
        if not (book and book.bids and book.asks): continue

        pos = exchange.get_positions()[sid]
        best_bid, best_ask = book.bids[0].price, book.asks[0].price
        mid = (best_bid + best_ask) / 2
        state = sentiment_states[sid]

        sent = get_agg_sentiment(SENTIMENT_HISTORY[sid], sid)

        # Stabilize on neg - shorter threshold and wait
        if sent < -0.3 or now < state['wait_until']:  # Less negative to trigger
            if sent < -0.3: 
                state['wait_until'] = now + STABILIZATION_WAIT
                print(f"{sid}: Neg sent {sent:.2f} - Wait {STABILIZATION_WAIT}s")
            continue

        # PID for retreat: target pos=0, error=pos
        error = pos - 0  # Target neutral
        pid_retreat = pid_controllers[sid].update(error)
        theo = mid - 0.01 * pos + pid_retreat * mid  # Stronger base retreat
        theo = clamp_price(theo)  # Clamp theoretical price

        # Sentiment overlay: more extreme widen/tighten credit
        credit_mult = 0.5 if sent > 0.3 else 1.5 if sent < -0.2 else 1.0
        credit = BASE_CREDIT * credit_mult
        bid_vol = max(0, min(QUOTED_VOLUME + sent * 5, POSITION_LIMIT - pos))  # Clamp to long limit
        ask_vol = max(0, min(QUOTED_VOLUME - sent * 5, pos + POSITION_LIMIT))  # Clamp to short limit

        b_price = round_down_to_tick(theo - credit, instrument.tick_size)
        a_price = round_up_to_tick(theo + credit, instrument.tick_size)

        print(f"{sid}: Pos={pos:.0f}, PID Retreat={pid_retreat:.4f}, Sent={sent:.2f}, Quotes: B{b_price:.2f}/V{bid_vol} A{a_price:.2f}/V{ask_vol} | Credit={credit:.2f}")

        insert_quotes(exchange, instrument, b_price, a_price, int(bid_vol), int(ask_vol))
        time.sleep(0.1)  # Faster loop