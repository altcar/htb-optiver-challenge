import datetime as dt
import time
from math import floor, ceil

from optibook.synchronous_client import Exchange
from sentiment import StockPredictor


def print_positions_and_pnl(exchange):
    positions = exchange.get_positions()
    pnl = exchange.get_pnl()

    print("Positions:")
    for instrument_id in positions:
        print(f"{instrument_id:>12s} : {positions[instrument_id]:>5}")

    print(f"PnL: {pnl:>8.2f}")


def round_down_to_tick(price, tick_size):
    return floor(price / tick_size) * tick_size


def round_up_to_tick(price, tick_size):
    return ceil(price / tick_size) * tick_size


def insert_quotes(exchange, instrument, bid_price, ask_price, bid_volume, ask_volume):
    if bid_volume > 0:
        exchange.insert_order(
            instrument_id=instrument.instrument_id,
            price=bid_price,
            volume=bid_volume,
            side="bid",
            order_type="limit",
        )
        time.sleep(0.05)

    if ask_volume > 0:
        exchange.insert_order(
            instrument_id=instrument.instrument_id,
            price=ask_price,
            volume=ask_volume,
            side="ask",
            order_type="limit",
        )
        time.sleep(0.05)


def make_prefix_map(instruments_dict):
    mp = {}
    for inst_id in instruments_dict:
        base = inst_id.split("_")[0]
        mp.setdefault(base, []).append(inst_id)
    return mp


# ------------------- CONFIG (from pdf style) -------------------
QUOTED_VOLUME = 10
FIXED_MINIMUM_CREDIT = 0.15
PRICE_RETREAT_PER_LOT = 0.005
POSITION_LIMIT = 100

NEWS_COOLDOWN_SECONDS = 15
SENT_NEUTRAL_BAND = 0.01      # <1% → keep quoting
SENT_STRONG_BAND = 0.03       # >3% → longer cooldown
# ---------------------------------------------------------------

exchange = Exchange()
exchange.connect()

INSTRUMENTS = exchange.get_instruments()
PREFIX_MAP = make_prefix_map(INSTRUMENTS)

predictor = StockPredictor()

# instrument_id -> unix timestamp until which we DO NOT quote
halt_until = {iid: 0.0 for iid in INSTRUMENTS}


while True:
    print("")
    print("-----------------------------------------------------------")
    print(f"TRADE LOOP ITERATION ENTERED AT {str(dt.datetime.utcnow()):18s} UTC.")
    print("-----------------------------------------------------------")

    now = time.time()

    # 1) NEWS + SENTIMENT
    feeds = exchange.poll_new_social_media_feeds()
    if feeds:
        for feed in feeds:
            preds = predictor.predict(feed.post)  # e.g. {'AAPL': +0.023, 'IBM': -0.011, ...}

            for base_code, pct_change in preds.items():
                if abs(pct_change) < SENT_NEUTRAL_BAND:
                    # neutral → do nothing
                    continue

                affected_instruments = PREFIX_MAP.get(base_code, [])

                for inst_id in affected_instruments:
                    # 1) always pull, even if already halted
                    exchange.delete_orders(inst_id)

                    # 2) try to flatten current risk again
                    positions = exchange.get_positions()
                    pos = positions.get(inst_id, 0)
                    if pos != 0:
                        book = exchange.get_last_price_book(inst_id)
                        if book and book.bids and book.asks:
                            if pos > 0:
                                # long → sell at best bid
                                px = book.bids[0].price
                                exchange.insert_order(
                                    instrument_id=inst_id,
                                    price=px,
                                    volume=pos,
                                    side="ask",
                                    order_type="ioc",
                                )
                            else:
                                # short → buy at best ask
                                px = book.asks[0].price
                                exchange.insert_order(
                                    instrument_id=inst_id,
                                    price=px,
                                    volume=abs(pos),
                                    side="bid",
                                    order_type="ioc",
                                )
                            time.sleep(0.05)

                    # 3) extend cooldown (later news pushes timer forward)
                    extra = NEWS_COOLDOWN_SECONDS
                    if abs(pct_change) >= SENT_STRONG_BAND:
                        extra = NEWS_COOLDOWN_SECONDS * 2

                    halt_until[inst_id] = time.time() + extra
                    print(f"{inst_id} -- NEWS HALT until {halt_until[inst_id]} (sent={pct_change:.3f})")

    # 2) NORMAL QUOTING (positional retreats)
    positions = exchange.get_positions()

    for instrument in INSTRUMENTS.values():
        inst_id = instrument.instrument_id

        # skip if in news cooldown
        if now < halt_until.get(inst_id, 0):
            print(f"{inst_id:>10s} -- NEWS HALT, not quoting")
            continue

        # start clean (pdf style)
        exchange.delete_orders(inst_id)

        order_book = exchange.get_last_price_book(inst_id)
        if not (order_book and order_book.bids and order_book.asks):
            print(f"{inst_id:>10s} -- INCOMPLETE ORDER BOOK")
            continue

        best_bid = order_book.bids[0].price
        best_ask = order_book.asks[0].price
        mid = (best_bid + best_ask) / 2.0

        pos = positions.get(inst_id, 0)

        # positional retreat
        theo = mid - PRICE_RETREAT_PER_LOT * pos

        bid_price = round_down_to_tick(
            theo - FIXED_MINIMUM_CREDIT, instrument.tick_size
        )
        ask_price = round_up_to_tick(
            theo + FIXED_MINIMUM_CREDIT, instrument.tick_size
        )

        bid_vol = QUOTED_VOLUME
        ask_vol = QUOTED_VOLUME

        # respect limits
        if pos + bid_vol > POSITION_LIMIT:
            bid_vol = max(0, POSITION_LIMIT - pos)
        if pos - ask_vol < -POSITION_LIMIT:
            ask_vol = max(0, POSITION_LIMIT + pos)

        print(
            f"{inst_id:>10s} -- mid={mid:>6.2f} theo={theo:>6.2f} "
            f"bid={bid_price:>6.2f} ask={ask_price:>6.2f} pos={pos:>4d} "
            f"vol=({bid_vol},{ask_vol})"
        )

        insert_quotes(exchange, instrument, bid_price, ask_price, bid_vol, ask_vol)

        time.sleep(0.5)

    time.sleep(3)
