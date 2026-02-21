import asyncio
import datetime as dt
import logging
from math import floor, ceil
from collections import deque

from optibook.synchronous_client import Exchange

# ----------------------------
# LOGGING SETUP
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")
logger.setLevel(logging.INFO)

# ----------------------------
# HELPER FUNCTIONS
# ----------------------------
def round_down_to_tick(price, tick_size):
    return floor(price / tick_size) * tick_size

def round_up_to_tick(price, tick_size):
    return ceil(price / tick_size) * tick_size

async def safe_exchange_call(func, *args, **kwargs):
    """Run synchronous exchange call safely in a thread, with reconnect."""
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except AssertionError as e:
        logger.warning(f"Exchange disconnected: {e}")
        try:
            exchange.connect()
            return await asyncio.to_thread(func, *args, **kwargs)
        except Exception as e2:
            logger.error(f"Reconnect failed: {e2}")
            return None
    except Exception as e:
        logger.error(f"Exchange call failed: {e}")
        return None

async def insert_order_safe(exchange, instrument_id, price, volume, side):
    if volume <= 0:
        return
    await safe_exchange_call(
        exchange.insert_order,
        instrument_id=instrument_id,
        price=price,
        volume=volume,
        side=side,
        order_type="limit"
    )

# ----------------------------
# SIGNALS
# ----------------------------
def detect_momentum(history, horizon=3):
    """Simple momentum over last horizon bars."""
    if len(history) < horizon:
        return 0
    mids = [(bids[0].price + asks[0].price)/2 for bids, asks in list(history)[-horizon:]]
    return (mids[-1] - mids[0]) * 0.5

def detect_mean_reversion(history, horizon=5):
    """Return difference from short-term average to mid price."""
    if len(history) < horizon:
        return 0
    mids = [(bids[0].price + asks[0].price)/2 for bids, asks in list(history)[-horizon:]]
    avg = sum(mids)/len(mids)
    return avg - mids[-1]

def estimate_volatility(history, horizon=5):
    if len(history) < horizon:
        return 0.01
    mids = [(bids[0].price + asks[0].price)/2 for bids, asks in list(history)[-horizon:]]
    returns = [mids[i+1]-mids[i] for i in range(len(mids)-1)]
    return max(0.01, sum([abs(r) for r in returns])/len(returns))

def risk_adjustment(position, volatility, position_limit, performance_factor):
    """Volatility- and position-aware risk scaling."""
    return max(
        0.1,
        (1 - abs(position)/position_limit) * performance_factor * (1/(1+volatility*10))
    )

def calculate_fair_price(position, history, order_book, position_limit, performance_factor):
    """Hybrid momentum + mean-reversion fair price calculation."""
    short_mom = detect_momentum(history, 3)
    med_mom = detect_momentum(history, 6)
    mean_rev = detect_mean_reversion(history, 5)

    signal_bias = 0.7*short_mom + 0.2*med_mom + 0.5*mean_rev

    best_bid, best_ask = order_book.bids[0].price, order_book.asks[0].price
    mid_price = (best_bid + best_ask)/2

    # Position retreat
    retreat = 0.01 * (position / position_limit)**3
    fair_price = mid_price - retreat*position + signal_bias

    # Volatility-based spread
    volatility = estimate_volatility(history, 5) * 0.5
    spread = 0.15 * (1 + abs(position)/position_limit + volatility)

    # Skew for aligned momentum
    skew = 0
    if short_mom * med_mom > 0:
        skew += 0.5 * abs(short_mom)
    fair_price += skew

    factor = risk_adjustment(position, volatility, position_limit, performance_factor)
    confidence = min(max(abs(signal_bias)+abs(skew),0.1),2.0)

    return fair_price, spread, factor, confidence

# ----------------------------
# CONFIG
# ----------------------------
BASE_QUOTED_VOLUME = 10
POSITION_LIMIT = 100
MAIN_LOOP_DELAY = 2  # throttle

# ----------------------------
# MARKET MAKER CLASS
# ----------------------------
class MarketMakerBot:
    def __init__(self):
        self.exchange = Exchange()
        self.exchange.connect()
        self.instruments = self.exchange.get_instruments()
        self.instrument_ids = list(self.instruments.keys())
        self.order_book_history = {inst: deque(maxlen=20) for inst in self.instrument_ids}
        self.performance_factor = 1.0

    async def run(self):
        while True:
            start_time = dt.datetime.utcnow()
            logger.info(f"\n{'-'*60}\nTRADE LOOP ITERATION AT {start_time}\n{'-'*60}")

            positions = await safe_exchange_call(self.exchange.get_positions) or {}
            pnl = await safe_exchange_call(self.exchange.get_pnl) or 0
            positions = {inst: max(-POSITION_LIMIT, min(POSITION_LIMIT, pos)) for inst, pos in positions.items()}

            logger.info(f"Positions: {positions}")
            logger.info(f"PnL: {pnl:.2f}")

            tasks = []
            for inst_id in self.instrument_ids:
                order_book = await safe_exchange_call(self.exchange.get_last_price_book, inst_id)
                if not (order_book and order_book.bids and order_book.asks):
                    continue

                history = self.order_book_history[inst_id]
                history.append((order_book.bids, order_book.asks))
                position = positions.get(inst_id, 0)

                fair_price, spread, factor, confidence = calculate_fair_price(
                    position, history, order_book, POSITION_LIMIT, self.performance_factor
                )

                bid_price = round_down_to_tick(fair_price - spread/2, self.instruments[inst_id].tick_size)
                ask_price = round_up_to_tick(fair_price + spread/2, self.instruments[inst_id].tick_size)

                bid_volume = min(int(BASE_QUOTED_VOLUME*confidence*factor), max(0, POSITION_LIMIT - position))
                ask_volume = min(int(BASE_QUOTED_VOLUME*confidence*factor), max(0, POSITION_LIMIT + position))

                logger.info(f"{inst_id:>6s} -- bid {bid_price:.2f} ask {ask_price:.2f} | Vol: {bid_volume}/{ask_volume}")

                if bid_volume > 0:
                    tasks.append(insert_order_safe(self.exchange, inst_id, bid_price, bid_volume, "bid"))
                if ask_volume > 0:
                    tasks.append(insert_order_safe(self.exchange, inst_id, ask_price, ask_volume, "ask"))

            if tasks:
                await asyncio.gather(*tasks)

            # Smooth performance factor update
            new_factor = max(0.2, min(1.5, pnl/100 + 1.0))
            self.performance_factor = 0.9*self.performance_factor + 0.1*new_factor

            elapsed = (dt.datetime.utcnow() - start_time).total_seconds()
            await asyncio.sleep(max(MAIN_LOOP_DELAY, 2 - elapsed))

# ----------------------------
# RUN BOT
# ----------------------------
if __name__ == "__main__":
    bot = MarketMakerBot()
    asyncio.run(bot.run())
