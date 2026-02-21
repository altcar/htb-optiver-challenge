import asyncio
import datetime as dt
import logging
import numpy as np
from optibook.synchronous_client import Exchange

logging.getLogger("client").setLevel("ERROR")

# ----------------------------
# CONFIGURATION
# ----------------------------
POSITION_LIMIT = 100           # max position per instrument
HISTORY_WINDOW = 50            # number of historical mid-prices to keep
REBALANCE_INTERVAL = 0.1        # seconds between rebalances
RISK_FREE_RATE = 0.0           # simplified Sharpe
BASE_ORDER_SIZE = 10

# ----------------------------
# HELPER FUNCTIONS
# ----------------------------
def compute_returns(prices):
    """Compute simple returns from a list of prices"""
    if len(prices) < 2:
        return np.array([0])
    return np.diff(prices) / prices[:-1]

def sharpe_ratio(weights, returns_matrix):
    """Compute portfolio Sharpe ratio"""
    if returns_matrix.shape[0] < 2:
        return 0.0
    port_returns = returns_matrix @ weights
    mean = np.mean(port_returns) - RISK_FREE_RATE
    std = np.std(port_returns)
    return mean / (std + 1e-6)

def project_position(weight, capital, price):
    """Map weight [-1,1] to number of lots respecting position limit"""
    return int(np.clip(weight * capital / price, -POSITION_LIMIT, POSITION_LIMIT))

# ----------------------------
# ASYNC MARKET FUNCTIONS
# ----------------------------
async def safe_exchange_call(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except AssertionError as e:
        print(f"Exchange disconnected. Reconnecting... ({e})")
        try:
            exchange.connect()
            return func(*args, **kwargs)
        except Exception as e2:
            print(f"Reconnect failed: {e2}")
            return None
    except Exception as e:
        print(f"Exchange call failed: {e}")
        return None

async def place_orders(instrument_id, target_position, current_position):
    """Place orders to move from current to target position"""
    delta = target_position - current_position
    if delta == 0:
        return
    side = "bid" if delta > 0 else "ask"
    volume = abs(delta)
    
    # Directly await async safe call
    await safe_exchange_call(
        exchange.insert_order,
        instrument_id=instrument_id,
        price=0,               # marketable order
        volume=volume,
        side=side,
        order_type="market"
    )

# ----------------------------
# MAIN LOOP
# ----------------------------
async def sharpe_portfolio_bot():
    global exchange
    exchange = Exchange()
    exchange.connect()
    instruments = list(exchange.get_instruments().keys())
    history = {inst: [] for inst in instruments}

    while True:
        # 1. Fetch mid-prices and maintain history
        mid_prices = []
        for inst in instruments:
            ob = await safe_exchange_call(exchange.get_last_price_book, inst)
            if ob and ob.bids and ob.asks:
                mid = (ob.bids[0].price + ob.asks[0].price) / 2
            else:
                mid = history[inst][-1] if history[inst] else 0
            history[inst].append(mid)
            if len(history[inst]) > HISTORY_WINDOW:
                history[inst].pop(0)
            mid_prices.append(mid)

        mid_prices = np.array(mid_prices)
        returns_matrix = np.array([compute_returns(history[inst]) for inst in instruments]).T
        if returns_matrix.shape[0] < 2:
            await asyncio.sleep(REBALANCE_INTERVAL)
            continue

        # 2. Optimize weights to maximize Sharpe ratio
        n = len(instruments)
        best_sharpe = -np.inf
        best_weights = np.zeros(n)
        for _ in range(1000):  # random search
            w = np.random.uniform(-1, 1, n)
            w /= np.sum(np.abs(w)) + 1e-6  # normalize to sum of abs 1
            sr = sharpe_ratio(w, returns_matrix)
            if sr > best_sharpe:
                best_sharpe = sr
                best_weights = w

        # 3. Calculate target positions
        capital = POSITION_LIMIT
        target_positions = [project_position(w, capital, p) for w, p in zip(best_weights, mid_prices)]

        # 4. Fetch current positions
        positions = await safe_exchange_call(exchange.get_positions) or {}
        tasks = []
        for inst, tgt_pos in zip(instruments, target_positions):
            cur_pos = positions.get(inst, 0)
            if cur_pos != tgt_pos:
                tasks.append(place_orders(inst, tgt_pos, cur_pos))

        # 5. Execute orders concurrently
        if tasks:
            await asyncio.gather(*tasks)

        # 6. Log for debugging
        print(f"{dt.datetime.now()} | Target positions: {dict(zip(instruments, target_positions))} | Sharpe={best_sharpe:.2f}")
        await asyncio.sleep(REBALANCE_INTERVAL)

# ----------------------------
# RUN BOT
# ----------------------------
asyncio.run(sharpe_portfolio_bot())
