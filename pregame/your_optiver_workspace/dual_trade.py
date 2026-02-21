import datetime as dt
import time
import logging

from optibook.synchronous_client import Exchange
from math import floor, ceil

logging.getLogger("client").setLevel("ERROR")


def print_positions_and_pnl(exchange):
    positions = exchange.get_positions()
    pnl = exchange.get_pnl()

    print("Positions:")
    for instrument_id in positions:
        print(f"  {instrument_id:10s}: {positions[instrument_id]:4.0f}")

    print(f"\nPnL: {pnl:.2f}")


def round_down_to_tick(price, tick_size):
    """
    Rounds a price down to the nearest tick, e.g. if the tick size is 0.10, a price of 0.97 will get rounded to 0.90.
    """
    return floor(price / tick_size) * tick_size


def round_up_to_tick(price, tick_size):
    """
    Rounds a price up to the nearest tick, e.g. if the tick size is 0.10, a price of 1.34 will get rounded to 1.40.
    """
    return ceil(price / tick_size) * tick_size


def insert_quotes(exchange, instrument, bid_price, ask_price, bid_volume, ask_volume):
    if bid_volume > 0:
        # Insert new bid limit order on the market
        exchange.insert_order(
            instrument_id=instrument.instrument_id,
            price=bid_price,
            volume=bid_volume,
            side="bid",
            order_type="limit",
        )

        # Wait for some time to avoid breaching the exchange frequency limit
        time.sleep(0.05)

    if ask_volume > 0:
        # Insert new ask limit order on the market
        exchange.insert_order(
            instrument_id=instrument.instrument_id,
            price=ask_price,
            volume=ask_volume,
            side="ask",
            order_type="limit",
        )

        # Wait for some time to avoid breaching the exchange frequency limit
        time.sleep(0.05)


exchange = Exchange()
exchange.connect()

INSTRUMENTS = exchange.get_instruments()

QUOTED_VOLUME = 10
FIXED_MINIMUM_CREDIT = 0.15
PRICE_RETREAT_PER_LOT = 0.005
CANCEL_SPREAD_THRESHOLD = 0.30
POSITION_LIMIT = 100
MAX_VOLUME = 50
LOOP_WAIT = 1.0
LIST_SPREAD = 0.15
TICK_SIZE = 0.10
VOLUME_PER_ORDER = 10

while True:
    print(f"")
    print(f"-----------------------------------------------------------------")
    print(f"TRADE LOOP ITERATION ENTERED AT {str(dt.datetime.now()):18s} UTC.")
    print(f"-----------------------------------------------------------------")

    print_positions_and_pnl(exchange)
    print(f"")
    print(f"          (ourbid) mktbid :: mktask (ourask)")

    order_book = exchange.get_last_price_book("NVDA_DUAL")
    hedge_book = exchange.get_last_price_book("NVDA")

    if not (hedge_book and hedge_book.bids and hedge_book.asks):
        print("Order book for NVDA has no bids or offers")
        continue

    if not (order_book and order_book.bids and order_book.asks):
        print("Order book for NVDA_DUAL has no bids or offers")
        continue

    best_bid = order_book.bids[0].price
    best_ask = order_book.asks[0].price

    dual_best_bid = hedge_book.bids[0].price
    dual_best_ask = hedge_book.asks[0].price


    if best_ask < dual_best_bid:
        # Sell dual (B), buy original (A)
        dual_side, dual_price = 'ask', dual_best_bid
        hedge_side, hedge_price = 'bid', 
        
    elif dual_best_ask < best_bid:
        # Buy dual (B), sell original (A)
        dual_side, dual_price = 'bid', dual_best_ask
        hedge_side, hedge_price = 'ask', best_bid

    