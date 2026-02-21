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
POSITION_LIMIT = 100

while True:
    print(f"")
    print(f"-----------------------------------------------------------------")
    print(f"TRADE LOOP ITERATION ENTERED AT {str(dt.datetime.now()):18s} UTC.")
    print(f"-----------------------------------------------------------------")

    # Display our own current positions in all stocks, and our PnL so far
    print_positions_and_pnl(exchange)
    print(f"")
    print(f"          (ourbid) mktbid :: mktask (ourask)")

    for instrument in INSTRUMENTS.values():
        # Remove all existing (still) outstanding limit orders
        exchange.delete_orders(instrument.instrument_id)

        # Obtain order book and only skip this instrument if there are no bids or offers available at all on that instrument,
        # as we we want to use the mid price to determine our own quoted price
        instrument_order_book = exchange.get_last_price_book(instrument.instrument_id)
        if not (
            instrument_order_book
            and instrument_order_book.bids
            and instrument_order_book.asks
        ):
            print(f"{instrument.instrument_id:>6s} --     INCOMPLETE ORDER BOOK")
            continue

        # Obtain own current position in instrument
        position = exchange.get_positions()[instrument.instrument_id]

        # Obtain best bid and ask prices from order book to determine mid price
        best_bid_price = instrument_order_book.bids[0].price
        best_ask_price = instrument_order_book.asks[0].price
        mid_price = (best_bid_price + best_ask_price) / 2.0

        # Calculate our fair/theoretical price based on the market mid price and our current position
        theoretical_price = mid_price - PRICE_RETREAT_PER_LOT * position

        # Calculate final bid and ask prices to insert
        bid_price = round_down_to_tick(
            theoretical_price - FIXED_MINIMUM_CREDIT, instrument.tick_size
        )
        ask_price = round_up_to_tick(
            theoretical_price + FIXED_MINIMUM_CREDIT, instrument.tick_size
        )

        # Calculate bid and ask volumes to insert, taking into account the exchange position_limit
        max_volume_to_buy = POSITION_LIMIT - position
        max_volume_to_sell = POSITION_LIMIT + position

        bid_volume = min(QUOTED_VOLUME, max_volume_to_buy)
        ask_volume = min(QUOTED_VOLUME, max_volume_to_sell)

        # Display information for tracking the algorithm's actions
        print(
            f"{instrument.instrument_id:>6s} -- ({bid_price:>6.2f}) {best_bid_price:>6.2f} :: {best_ask_price:>6.2f} ({ask_price:>6.2f})"
        )

        # Insert new quotes
        insert_quotes(
            exchange, instrument, bid_price, ask_price, bid_volume, ask_volume
        )

    # Wait for a few seconds to refresh the quotes
    print(f"\nWaiting for 2 seconds.")
    time.sleep(2)
