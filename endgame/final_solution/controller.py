# integrated_controller.py
import time
import logging
from queue import Queue
from collections import defaultdict, deque, namedtuple
from math import tanh
from optibook.synchronous_client import Exchange

from dual_strategy import dual_strategy_multi
from structure import OrderDetail, check_breach_position_limit


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logging.getLogger("client").setLevel("ERROR")

STOCK_DUAL_PAIRS = [
    ("NVDA", "NVDA_DUAL"),
    ("ING", "ING_DUAL"),
    ("SAN", "SAN_DUAL"),
    ("CSCO", "CSCO_DUAL"),
    ("PFE", "PFE_DUAL"),
]

POSITION_LIMIT = 100
OUTSTANDING_VOLUME_LIMIT = 400

BASE_MM_SPREAD = 0.22
MIN_PROFITABLE_SPREAD = 0.8
MAX_SPREAD = 3
HEDGE_BATCH_SIZE = 30
URGENT_HEDGE_THRESHOLD = 70
HEDGE_SPREAD = 0.03
POSITION_BUFFER = 10

PositionInfo = namedtuple("PositionInfo", ["volume", "timestamp"])

class PairTracker:
    """Track actual P&L"""
    def __init__(self):
        self.buys = []
        self.sells = []
        self.realized_pnl = 0.0
        
    def record_trade(self, side, price, volume):
        """Record trades"""
        if side == 'buy':
            self.buys.append((price, volume))
        else:
            self.sells.append((price, volume))
        self._calculate_realized()
    
    def _calculate_realized(self):
        """Calculate realized P&L"""
        self.realized_pnl = 0.0
        buy_queue = []
        for price, vol in self.buys:
            buy_queue.extend([price] * int(vol))
        sell_queue = []
        for price, vol in self.sells:
            sell_queue.extend([price] * int(vol))
        matched = min(len(buy_queue), len(sell_queue))
        for i in range(matched):
            self.realized_pnl += (sell_queue[i] - buy_queue[i])
    
    def get_pnl(self):
        return self.realized_pnl

def mid_from_book(book):
    """Get mid price"""
    if not book:
        return None
    if book.bids and book.asks:
        return (book.bids[0].price + book.asks[0].price) / 2
    elif book.bids:
        return book.bids[0].price
    elif book.asks:
        return book.asks[0].price
    return None


def get_market_spread(book):
    """Get current market spread"""
    if not (book and book.bids and book.asks):
        return 0.20
    return book.asks[0].price - book.bids[0].price

def calculate_profitable_spread(market_spread, position, position_limit):
    """Inventory-aware spread adjustment with smooth skewing"""
    base_spread = max(BASE_MM_SPREAD, market_spread * 0.55)
    inv = position / position_limit
    skew = tanh(inv)
    lean_strength = 0.3

    bid_spread = base_spread * (1 + lean_strength * skew)
    ask_spread = base_spread * (1 - lean_strength * skew)

    return bid_spread, ask_spread

    
    bid_spread = max(MIN_PROFITABLE_SPREAD, min(MAX_SPREAD, bid_spread))
    ask_spread = max(MIN_PROFITABLE_SPREAD, min(MAX_SPREAD, ask_spread))
    
    return bid_spread, ask_spread


def calculate_optimal_volume(position, outstanding_orders, position_limit):
    """Calculate optimal volume for MM"""
    available_long = position_limit - POSITION_BUFFER - position
    available_short = position_limit - POSITION_BUFFER + position
    
    outstanding_buy = sum(o.volume for o in outstanding_orders.values() if o.side == 'bid')
    outstanding_sell = sum(o.volume for o in outstanding_orders.values() if o.side == 'ask')
    
    available_long = max(0, available_long - outstanding_buy)
    available_short = max(0, available_short - outstanding_sell)

    
    return max(0, available_long), max(0, available_short)


def should_hedge_now(net_position):
    """Smart hedging: only when necessary"""
    abs_net = abs(net_position)
    
    if abs_net >= URGENT_HEDGE_THRESHOLD:
        return True, "URGENT"
    if abs_net >= HEDGE_BATCH_SIZE:
        return True, "BATCH"
    
    return False, "WAITING"


def execute_smart_hedge(exchange, hedge_id, net_position, hedge_book, positions, outstanding_orders, urgency):
    """Execute hedge with position limit checking"""
    if net_position == 0:
        return False
    
    volume = abs(net_position)
    side = "bid" if net_position < 0 else "ask"
    
    current_hedge_pos = positions.get(hedge_id, 0)
    hedge_outstanding = outstanding_orders.get(hedge_id, {})
    
    if side == "bid":
        outstanding_buy = sum(o.volume for o in hedge_outstanding.values() if o.side == 'bid')
        max_safe = POSITION_LIMIT - POSITION_BUFFER - current_hedge_pos - outstanding_buy
    else:
        outstanding_sell = sum(o.volume for o in hedge_outstanding.values() if o.side == 'ask')
        max_safe = POSITION_LIMIT - POSITION_BUFFER + current_hedge_pos - outstanding_sell
    
    volume = min(volume, max(0, max_safe))
    
    if volume <= 0:
        return False
    
    mid = mid_from_book(hedge_book)
    if not mid:
        return False

    if side == "bid":
        price =  hedge_book.asks[0].price if hedge_book and hedge_book.asks else mid 
    else:
            price = hedge_book.bids[0].price if hedge_book and hedge_book.bids else mid

    try:
        volume = int(volume)
        
        oid = exchange.insert_order(
            instrument_id=hedge_id,
            price=float(price),
            volume=volume,
            side=side,
            order_type="ioc",
        )
        logging.info(f'🛡️  [HEDGE-{urgency}] {side} {hedge_id} {volume} @ {price:.2f}')
        return True
    except Exception as e:
        logging.error(f'❌ [HEDGE] {e}')
        return False

def main():
    exchange = Exchange()
    exchange.connect()

    order_queue = Queue()
    delete_order_queue = Queue()

    prev_positions = {}
    pair_trackers = {trade_id: PairTracker() for trade_id, _ in STOCK_DUAL_PAIRS}
    
    loop_count = 0
    last_pnl_print = time.time()

    logging.info("Market Making Controller Started")

    while True:
        try:
            loop_count += 1
            current_time = time.time()
            
            # Get market data
            order_books = {}
            for trade_id, hedge_id in STOCK_DUAL_PAIRS:
                order_books[trade_id] = exchange.get_last_price_book(trade_id)
                order_books[hedge_id] = exchange.get_last_price_book(hedge_id)

            positions = exchange.get_positions()
            if not prev_positions:
                prev_positions = dict(positions)

            outstanding_orders = {}
            for trade_id, hedge_id in STOCK_DUAL_PAIRS:
                outstanding_orders[trade_id] = exchange.get_outstanding_orders(trade_id)
                outstanding_orders[hedge_id] = exchange.get_outstanding_orders(hedge_id)

            # MARKET MAKING
            for trade_id, hedge_id in STOCK_DUAL_PAIRS:
                current_pos = positions.get(trade_id, 0)
                book = order_books[trade_id]
                outs = outstanding_orders[trade_id]
                
                if not book:
                    continue
                
                market_spread = get_market_spread(book)
                bid_spread, ask_spread = calculate_profitable_spread(
                    market_spread=market_spread,
                    position=current_pos,
                    position_limit=POSITION_LIMIT
                )
                avg_spread = (bid_spread + ask_spread) / 2
                
                buy_volume, sell_volume = calculate_optimal_volume(
                    position=current_pos,
                    outstanding_orders=outs,
                    position_limit=POSITION_LIMIT
                )
                
                avg_volume = (buy_volume + sell_volume) // 2
                avg_volume = max(10, avg_volume)
                
                dual_strategy_multi(
                    order_books={trade_id: book, hedge_id: order_books[hedge_id]},
                    outstanding_orders={
                        trade_id: outs,
                        hedge_id: outstanding_orders[hedge_id]
                    },
                    positions=positions,
                    order_queue=order_queue,
                    delete_order_queue=delete_order_queue,
                    stock_dual_pairs=[(trade_id, hedge_id)],
                    default_list_spread=avg_spread,
                    default_cancel_spread_threshold=avg_spread * 3.0,
                    default_max_volume=avg_volume,
                    default_volume_per_order=max(10, avg_volume // 5),
                    position_limit=POSITION_LIMIT,
                )

    
            while not order_queue.empty():
                order = order_queue.get()
                
                instrument_outs = outstanding_orders.get(order.instrument_id, {})
                if check_breach_position_limit(order, positions, instrument_outs, POSITION_LIMIT):
                    continue
                
                try:
                    oid = exchange.insert_order(
                        instrument_id=order.instrument_id,
                        price=float(order.price),
                        volume=int(order.volume),
                        side=order.side,
                        order_type=order.order_type,
                    )
                except Exception as e:
                    if "position limit" in str(e).lower():
                        logging.error(f'[LIMIT] {order.instrument_id}: {e}')

      
            while not delete_order_queue.empty():
                instr_id, oid = delete_order_queue.get()
                try:
                    exchange.delete_order(instrument_id=instr_id, order_id=oid)
                except:
                    pass

            positions_now = exchange.get_positions()
            outstanding_now = {}
            for trade_id, hedge_id in STOCK_DUAL_PAIRS:
                outstanding_now[trade_id] = exchange.get_outstanding_orders(trade_id)
                outstanding_now[hedge_id] = exchange.get_outstanding_orders(hedge_id)

          
            for trade_id, hedge_id in STOCK_DUAL_PAIRS:
                trade_pos = positions_now.get(trade_id, 0)
                hedge_pos = positions_now.get(hedge_id, 0)
                net_position = trade_pos + hedge_pos
                
                should_hedge, urgency = should_hedge_now(net_position)
                
                if should_hedge:
                    hedge_book = order_books.get(hedge_id)
                    if hedge_book:
                        execute_smart_hedge(
                            exchange=exchange,
                            hedge_id=hedge_id,
                            net_position=net_position,
                            hedge_book=hedge_book,
                            positions=positions_now,
                            outstanding_orders=outstanding_now,
                            urgency=urgency
                        )

            prev_positions = positions_now

            time.sleep(.15)

        except KeyboardInterrupt:
            logging.info("Shutting down...")
            break
        except Exception as e:
            logging.error(f"Main loop error: {e}", exc_info=True)
            
            if "Cannot call function until connected" in str(e) or "connection" in str(e).lower():
                logging.warning("Connection lost. Attempting to reconnect...")
                try:
            
                    time.sleep(0.5) 
                    del(exchange)
                    exchange = Exchange()
                    exchange.connect()
                    logging.info("Successfully reconnected to the Exchange.")
                    
                    continue # Jumps back to the start of the while loop
                
                except Exception as reconnect_e:
                    logging.error(f"Reconnection failed: {reconnect_e}. Retrying in 5 seconds...")
                    time.sleep(5)
            
    
            else:
                time.sleep(1)


if __name__ == "__main__":
    main()