import asyncio
import datetime as dt
import logging
import time
from collections import deque
from math import floor, ceil

import numpy as np
from optibook.synchronous_client import Exchange

logging.getLogger("client").setLevel("ERROR")
log = logging.getLogger("bot")
log.setLevel(logging.INFO)

# ------------------------------------------------
# CONFIGURATION
# ------------------------------------------------
POSITION_LIMIT = 100
HISTORY_WINDOW = 50
REBALANCE_INTERVAL = 5

# For adaptive reconnect delay tuning (binary-search-like)
RECONNECT_LOW = 0.0
RECONNECT_HIGH = 2.0
reconnect_delay = (RECONNECT_LOW + RECONNECT_HIGH) / 2.0
reconnect_results = []
RECONNECT_SAMPLES = 4      # Number of attempts before adjusting
SUCCESS_THRESHOLD = 0.7    # Required success ratio to move upper bound down


# ------------------------------------------------
# PRICE UTILS
# ------------------------------------------------
def compute_mid_price(book):
    if book is None:
        return None
    if not book.bids or not book.asks:
        return None
    best_bid = book.bids[0].price
    best_ask = book.asks[0].price
    return (best_bid + best_ask) / 2.0


# ------------------------------------------------
# RECONNECT HANDLING WITH ADAPTIVE DELAY
# ------------------------------------------------
def test_connection_attempt(exchange):
    """
    Return True if exchange is connected *and* we can retrieve a price book for any instrument.
    """
    try:
        insts = exchange.get_instruments()
        if not insts:
            return False
        # Test one instrument
        name = list(insts.keys())[0]
        book = exchange.get_last_price_book(name)
        return book is not None
    except Exception:
        return False


def adapt_reconnect_delay(success):
    """
    Adaptive binary search adjustment of reconnect_delay.
    Collect samples, compute ratio, then adjust RECONNECT_LOW/HIGH.
    """
    global RECONNECT_LOW, RECONNECT_HIGH, reconnect_delay, reconnect_results

    reconnect_results.append(1 if success else 0)
    if len(reconnect_results) < RECONNECT_SAMPLES:
        return

    ratio = sum(reconnect_results) / len(reconnect_results)
    reconnect_results = []  # reset

    if ratio >= SUCCESS_THRESHOLD:
        # Success is good → try smaller delay
        RECONNECT_HIGH = reconnect_delay
    else:
        # Failure → require larger delay
        RECONNECT_LOW = reconnect_delay

    reconnect_delay = (RECONNECT_LOW + RECONNECT_HIGH) / 2.0
    log.info(f"[Adaptive Tuner] New reconnect delay = {reconnect_delay:.3f}s "
             f"(low={RECONNECT_LOW:.3f}, high={RECONNECT_HIGH:.3f}, ratio={ratio:.2f})")


def ensure_connected(exchange):
    """
    Ensure the exchange is connected.
    If not, attempt reconnect using adaptive reconnect delay logic.
    """
    global reconnect_delay

    while not exchange.is_connected():
        log.warning("Connection lost. Reconnecting...")
        try:
            exchange.connect()
            success = test_connection_attempt(exchange)
            adapt_reconnect_delay(success)

            if success:
                log.info("Reconnected.")
                return
            else:
                log.warning("Reconnected but no valid data. Retrying...")
        except Exception as e:
            log.error(f"Reconnect failed: {e}")

        time.sleep(reconnect_delay)


# ------------------------------------------------
# MAIN STRATEGY LOOP (SIMPLE MOMENTUM DEMO)
# ------------------------------------------------
def main():
    exchange = Exchange()
    ensure_connected(exchange)

    instruments = exchange.get_instruments()
    price_history = {name: deque(maxlen=HISTORY_WINDOW) for name in instruments.keys()}

    last_rebalance = time.time()

    while True:
  
        ensure_connected(exchange)

        for name in instruments.keys():
            try:
                book = exchange.get_last_price_book(name)
            except AssertionError:
                ensure_connected(exchange)
                print("ERR")
                continue

            mid = compute_mid_price(book)
            if mid is None:
                print("MIDNONE")
                continue

            price_history[name].append(mid)

        now = time.time()
        if now - last_rebalance >= REBALANCE_INTERVAL:
            last_rebalance = now

            current_positions = exchange.get_positions()
            target_positions = {}

            for name, history in price_history.items():
                if len(history) < 2:
                    target_positions[name] = 0
                    continue
                momentum = history[-1] - history[0]
                scale = 10  # tuning knob
                pos = max(-POSITION_LIMIT, min(POSITION_LIMIT, int(momentum * scale)))
                target_positions[name] = pos

            for name, target in target_positions.items():
                current = current_positions.get(name, 0)
                diff = target - current
                if diff == 0:
                    continue

                side = 'buy' if diff > 0 else 'sell'
                vol = abs(diff)

                try:
                    if side == 'buy':
                        book = exchange.get_last_price_book(name)
                        if book and book.asks:
                            px = book.asks[0].price
                            exchange.insert_order(name, price=px, volume=vol, side='buy')
                    else:
                        book = exchange.get_last_price_book(name)
                        if book and book.bids:
                            px = book.bids[0].price
                            exchange.insert_order(name, price=px, volume=vol, side='sell')
                except Exception:
                    ensure_connected(exchange)
                    print("ERR2")
                    continue

        time.sleep(0.1)


if __name__ == "__main__":
    main()
