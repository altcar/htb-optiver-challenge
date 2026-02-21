import time
import logging
from collections import deque, namedtuple
from math import exp
import numpy as np
import torch

from optibook.synchronous_client import Exchange
from transformers import pipeline

# -------------------------------
# Logging
# -------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logging.getLogger("client").setLevel("ERROR")

# -------------------------------
# Sentiment model
# -------------------------------
MODEL_NAME = "distilbert-base-uncased-finetuned-sst-2-english"
device = 0 if torch.cuda.is_available() else -1

sentiment_pipeline = pipeline(
    "sentiment-analysis",
    model=MODEL_NAME,
    tokenizer=MODEL_NAME,
    device=device,
    truncation=True
)

# -------------------------------
# Parameters
# -------------------------------
POSITION_LIMIT = 100
ROLLING_WINDOW = 20
MAX_HOLD_SECONDS = 5
LOOP_WAIT = 0.1
SENTIMENT_THRESHOLD = 0.05
MAX_KELLY_FRACTION = 0.5
MAX_TRADE_PER_ITER = 50
MIN_TRADE_INTERVAL = 0.2  # seconds per ticker

# Feedback & learning
PNL_HISTORY = deque(maxlen=100)
TICKER_MODEL = {}

# -------------------------------new
# Tracking structures
# -------------------------------
ticker_history = {}
open_positions = {}
last_price_cache = {}
open_orders = {}
LAST_TRADE_TS = {}
PositionInfo = namedtuple("PositionInfo", ["volume", "timestamp"])

# -------------------------------
# Exchange helper
# -------------------------------
def create_exchange():
    while True:
        try:
            ex = Exchange()
            ex.connect()
            logging.info("Connected to exchange.")
            return ex
        except Exception as e:
            logging.error(f"Failed to connect: {e}, retrying...")
            time.sleep(2)

exchange = create_exchange()
TICKERS = list(exchange.get_instruments().keys())
logging.info(f"Tickers: {TICKERS}")

for ticker in TICKERS:
    ticker_history[ticker] = deque(maxlen=ROLLING_WINDOW)
    open_positions[ticker] = None
    open_orders[ticker] = []
    TICKER_MODEL[ticker] = deque(maxlen=100)
    LAST_TRADE_TS[ticker] = 0

# -------------------------------
# Portfolio cleanup at startup
# -------------------------------
def liquidate_startup_positions():
    try:
        positions = exchange.get_positions()
    except:
        positions = {}
    for ticker, pos in positions.items():
        if pos == 0:
            continue
        book = exchange.get_last_price_book(ticker)
        if not book or not book.bids or not book.asks:
            continue
        side, price = ("ask", book.asks[0].price) if pos > 0 else ("bid", book.bids[0].price)
        try:
            exchange.insert_order(
                instrument_id=ticker,
                price=price,
                volume=int(abs(pos)),
                side=side,
                order_type="limit"
            )
            logging.info(f"Startup liquidation: Cleared {pos} {ticker} @ {price}")
        except Exception as e:
            logging.error(f"Startup liquidation error for {ticker}: {e}")

liquidate_startup_positions()

# -------------------------------
# Helper functions
# -------------------------------
def sentiment_to_score(headline: str) -> int:
    if not headline:
        return 0
    res = sentiment_pipeline(headline, top_k=None)[0]
    score = res['score'] * 100
    return int(score if res['label'] == "POSITIVE" else -score)

def get_z_score(ticker: str, new_score: float) -> float:
    history = ticker_history[ticker]
    history.append(new_score)
    if len(history) < 2:
        return 0.0
    return (new_score - np.mean(history)) / np.std(history) if np.std(history) > 0 else 0.0

def z_to_prob(z: float) -> float:
    return np.clip(1 / (1 + exp(-z)), 0.05, 0.95)

def kelly_position(z: float, position_limit: int, kelly_frac=MAX_KELLY_FRACTION) -> int:
    p = z_to_prob(z)
    f = np.clip((p - (1 - p)) / 1, -kelly_frac, kelly_frac)
    return int(f * position_limit)

def get_last_price_cached(ticker: str):
    now = time.time()
    if ticker not in last_price_cache or now - last_price_cache[ticker]['ts'] > 0.1:
        last_price_cache[ticker] = {'book': exchange.get_last_price_book(ticker), 'ts': now}
    return last_price_cache[ticker]['book']

def clamp_trade_volume(current_pos: int, target_pos: int, limit: int) -> int:
    return max(-limit, min(limit, target_pos))

def cancel_self_orders(ticker: str):
    for order_id in open_orders[ticker]:
        try:
            exchange.cancel_order(order_id)
        except:
            pass
    open_orders[ticker].clear()

# -------------------------------
# Self-learning & feedback
# -------------------------------
def update_feedback(trade_profit):
    PNL_HISTORY.append(trade_profit)
    avg_pnl = np.mean(PNL_HISTORY)

    global SENTIMENT_THRESHOLD, MAX_TRADE_PER_ITER

    if avg_pnl > 0.5:
        SENTIMENT_THRESHOLD *= 0.95
        MAX_TRADE_PER_ITER = min(MAX_TRADE_PER_ITER + 5, POSITION_LIMIT)
    elif avg_pnl < -0.5:
        SENTIMENT_THRESHOLD *= 1.05
        MAX_TRADE_PER_ITER = max(MAX_TRADE_PER_ITER - 5, 1)

    SENTIMENT_THRESHOLD = np.clip(SENTIMENT_THRESHOLD, 0.01, 0.2)

def learn_from_trade(ticker, score, profit):
    TICKER_MODEL[ticker].append((score, profit))

def predict_trade(ticker, score):
    history = TICKER_MODEL[ticker]
    if not history:
        return score
    weighted = sum(s * (p > 0) for s, p in history) / max(1, len(history))
    return (score + weighted) / 2

# -------------------------------
# Trade execution
# -------------------------------
def execute_trade(ticker: str, score: int, positions: dict):
    now = time.time()
    if now - LAST_TRADE_TS[ticker] < MIN_TRADE_INTERVAL:
        return  # throttle per-ticker trades

    current_pos = positions.get(ticker, 0)
    score = predict_trade(ticker, score)
    z = get_z_score(ticker, score)
    if abs(z) < SENTIMENT_THRESHOLD:
        return

    target_pos = clamp_trade_volume(current_pos, kelly_position(z, POSITION_LIMIT), POSITION_LIMIT)
    trade_volume = np.clip(target_pos - current_pos, -MAX_TRADE_PER_ITER, MAX_TRADE_PER_ITER)
    if trade_volume == 0:
        return

    # Avoid self-trade
    if open_positions[ticker] and np.sign(trade_volume) == np.sign(open_positions[ticker].volume):
        logging.info(f"Skipping trade for {ticker} to avoid self-trade")
        return

    side = "bid" if trade_volume > 0 else "ask"
    volume = abs(trade_volume)
    book = get_last_price_cached(ticker)
    if not book or not book.bids or not book.asks:
        return

    price = book.asks[0].price if side == "bid" else book.bids[0].price
    cancel_self_orders(ticker)

    try:
        order_id = exchange.insert_order(
            instrument_id=ticker,
            price=price,
            volume=volume,
            side=side,
            order_type="limit"
        )
        open_positions[ticker] = PositionInfo(volume=trade_volume, timestamp=time.time())
        open_orders[ticker].append(order_id)

        trade_profit = trade_volume * (price - (book.bids[0].price if side=="bid" else book.asks[0].price))
        learn_from_trade(ticker, score, trade_profit)
        update_feedback(trade_profit)
        LAST_TRADE_TS[ticker] = now
        logging.info(f"Executed {side} {volume} {ticker} @ {price:.2f} | Z={z:.2f}")
    except Exception as e:
        logging.error(f"Trade error {ticker}: {e}")

# -------------------------------
# Liquidation
# -------------------------------
def liquidate_position(ticker: str, positions: dict):
    info = open_positions.get(ticker)
    if not info or info.volume == 0:
        return

    current_pos = positions.get(ticker, 0)
    z = get_z_score(ticker, 0)
    holding_time = time.time() - info.timestamp

    if z * info.volume > 0 and holding_time <= MAX_HOLD_SECONDS:
        return

    side = "ask" if info.volume > 0 else "bid"
    volume = min(abs(info.volume), POSITION_LIMIT - abs(current_pos))
    book = get_last_price_cached(ticker)
    if not book or not book.bids or not book.asks:
        return

    price = book.asks[0].price if side == "bid" else book.bids[0].price
    cancel_self_orders(ticker)

    try:
        order_id = exchange.insert_order(
            instrument_id=ticker,
            price=price,
            volume=volume,
            side=side,
            order_type="limit"
        )
        open_positions[ticker] = None
        open_orders[ticker].append(order_id)
        logging.info(f"Liquidated {volume} {ticker} @ {price:.2f}")
    except Exception as e:
        logging.error(f"Liquidation error {ticker}: {e}")

# -------------------------------
# Main loop
# -------------------------------
while True:
    try:
        if not exchange.is_connected():
            logging.warning("Exchange disconnected, reconnecting...")
            exchange = create_exchange()
            time.sleep(1)
            continue

        positions = exchange.get_positions()
        pnl = exchange.get_pnl()
        logging.info(f"Positions: {positions} | PnL: {pnl:.2f}")

        feeds = exchange.poll_new_social_media_feeds()
        headline = (feeds[-1].post or "").strip() if feeds else ""
        score = sentiment_to_score(headline) if headline else 0

        if headline:
            logging.info(f"Headline: {headline} | Score: {score}")

        for ticker in TICKERS:
            execute_trade(ticker, score, positions)

        # Liquidate positions separately to avoid self-trades
        for ticker in TICKERS:
            liquidate_position(ticker, positions)

        time.sleep(LOOP_WAIT)

    except Exception as e:
        logging.error(f"Main loop error: {e}")
        time.sleep(1)
