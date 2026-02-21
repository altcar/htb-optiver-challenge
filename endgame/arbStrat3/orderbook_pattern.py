# integrated_controller_v2.py
import time
import logging
from queue import Queue
from collections import defaultdict, deque, namedtuple
from math import exp
import numpy as np
import torch
import json
import re

from optibook.synchronous_client import Exchange

from dual_strategy import dual_strategy_multi
from structure import OrderDetail, check_breach_position_limit

# -------------------------------
# Logging
# -------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logging.getLogger("client").setLevel("ERROR")

# -------------------------------
# Configuration
# -------------------------------
STOCK_DUAL_PAIRS = [
    ("NVDA", "NVDA_DUAL"),
    ("ING", "ING_DUAL"),
    ("SAN", "SAN_DUAL"),
    ("CSCO", "CSCO_DUAL"),
    ("PFE", "PFE_DUAL"),
]


POSITION_LIMIT = 100
OUTSTANDING_VOLUME_LIMIT = 400

# Market Making Parameters
BASE_MM_SPREAD = 0.22
MIN_PROFITABLE_SPREAD = 0.12
MAX_SPREAD = 0.50
HEDGE_BATCH_SIZE = 30
URGENT_HEDGE_THRESHOLD = 70
HEDGE_SPREAD = 0.03
POSITION_BUFFER = 10

# NEWS TRADING Parameters - MASSIVE SPREAD MODE
NEWS_MASSIVE_SPREAD = 5.0  # Absolutely massive spread when news comes in
NEWS_ORDER_VOLUME = 100  # Full volume on each side
NEWS_COOLDOWN_SECONDS = 5.0  # 5 seconds halt for massive spreads
NEWS_LOG_THROTTLE = 1.0  # Log every second during news mode

# -------------------------------
# Data Structures
# -------------------------------
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


class NewsState:
    """Track news event state - just timing, no sentiment"""
    def __init__(self):
        self.active = False
        self.trigger_time = 0
        self.last_log_time = 0  # For throttling logs
   
    def trigger_news(self, timestamp):
        """Activate massive spread mode"""
        self.active = True
        self.trigger_time = timestamp
        self.last_log_time = timestamp  # Log immediately on trigger
        
    def is_active(self, current_time):
        """Check if still in massive spread mode"""
        if not self.active:
            return False
        if current_time - self.trigger_time > NEWS_COOLDOWN_SECONDS:
            self.active = False
            return False
        return True
    
    def should_log(self, current_time):
        """Check if enough time has passed to log again"""
        if current_time - self.last_log_time >= NEWS_LOG_THROTTLE:
            self.last_log_time = current_time
            return True
        return False
    
    def get_time_remaining(self, current_time):
        """Get remaining time in news mode"""
        return NEWS_COOLDOWN_SECONDS - (current_time - self.trigger_time)
    
    def reset(self):
        """Reset news state"""
        self.active = False


# -------------------------------
# NEWS TRADING Functions
# -------------------------------

def cancel_all_orders(exchange, ticker, outstanding_orders):
    """Cancel all outstanding orders for a ticker"""
    outs = outstanding_orders.get(ticker, {})
    cancelled_count = 0
    for oid in list(outs.keys()):
        try:
            exchange.delete_order(instrument_id=ticker, order_id=oid)
            cancelled_count += 1
        except:
            pass
    return cancelled_count


def place_massive_spread_orders(exchange, ticker, book, current_position, outstanding_orders):
    """Place standing orders with absolutely massive spreads
    Returns True if orders were placed"""
    if not book or not book.bids or not book.asks:
        return False
    
    # Get mid price
    mid = (book.bids[0].price + book.asks[0].price) / 2
    
    # Calculate massive spread prices
    bid_price = mid - NEWS_MASSIVE_SPREAD
    ask_price = mid + NEWS_MASSIVE_SPREAD
    
    # Ensure prices are positive
    bid_price = max(0.01, bid_price)
    ask_price = max(bid_price + 0.02, ask_price)
    
    # Calculate available volumes respecting position limits
    available_long = POSITION_LIMIT - POSITION_BUFFER - current_position
    available_short = POSITION_LIMIT - POSITION_BUFFER + current_position
    
    # Account for outstanding orders
    outstanding_buy = sum(o.volume for o in outstanding_orders.values() if o.side == 'bid')
    outstanding_sell = sum(o.volume for o in outstanding_orders.values() if o.side == 'ask')
    
    available_long = max(0, available_long - outstanding_buy)
    available_short = max(0, available_short - outstanding_sell)
    
    bid_volume = min(NEWS_ORDER_VOLUME, available_long)
    ask_volume = min(NEWS_ORDER_VOLUME, available_short)
    
    orders_placed = False
    
    # Place bid order with massive spread
    if bid_volume > 0:
        try:
            exchange.insert_order(
                instrument_id=ticker,
                price=float(bid_price),
                volume=int(bid_volume),
                side="bid",
                order_type="limit",
            )
            orders_placed = True
        except Exception as e:
            logging.error(f'❌ [NEWS-BID] {ticker}: {e}')
    
    # Place ask order with massive spread
    if ask_volume > 0:
        try:
            exchange.insert_order(
                instrument_id=ticker,
                price=float(ask_price),
                volume=int(ask_volume),
                side="ask",
                order_type="limit",
            )
            orders_placed = True
        except Exception as e:
            logging.error(f'❌ [NEWS-ASK] {ticker}: {e}')
    
    if orders_placed:
        logging.info(
            f'📢 [NEWS-ORDERS] {ticker}: '
            f'BID {bid_volume}@{bid_price:.2f} | ASK {ask_volume}@{ask_price:.2f} | '
            f'Spread: {ask_price - bid_price:.2f}'
        )
    
    return orders_placed


def handle_news_mode(exchange, news_state, ticker, positions, outstanding_orders, order_books, current_time):
    """Handle massive spread orders during news mode
    1. Cancel all existing orders
    2. Place new orders with massive spreads"""
    if not news_state.is_active(current_time):
        return False
    
    book = order_books.get(ticker)
    if not book:
        return False
    
    should_log = news_state.should_log(current_time)
    
    # Step 1: Cancel all outstanding orders
    cancel_all_orders(exchange, ticker, outstanding_orders)
    
    # Step 2: Get current position
    current_position = positions.get(ticker, 0)
    
    # Step 3: Place massive spread orders
    place_massive_spread_orders(
        exchange=exchange,
        ticker=ticker,
        book=book,
        current_position=current_position,
        outstanding_orders=outstanding_orders.get(ticker, {})
    )
    
    # Log periodically
    if should_log:
        time_remaining = news_state.get_time_remaining(current_time)
        logging.info(
            f'📰 [NEWS-MODE] {ticker}: '
            f'Pos={current_position:+4d} | '
            f'Spread=±{NEWS_MASSIVE_SPREAD:.2f} | '
            f'Time={time_remaining:.2f}s'
        )
    
    return True


# -------------------------------
# Market Making Functions
# -------------------------------
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
    """Calculate spread that maximizes profit while managing inventory"""
    base_spread = max(BASE_MM_SPREAD, market_spread * 0.55)
    
    if position > 60:
        bid_spread = base_spread * 1.4
        ask_spread = base_spread * 0.75
    elif position < -60:
        bid_spread = base_spread * 0.75
        ask_spread = base_spread * 1.4
    elif position > 30:
        bid_spread = base_spread * 1.15
        ask_spread = base_spread * 0.85
    elif position < -30:
        bid_spread = base_spread * 0.85
        ask_spread = base_spread * 1.15
    else:
        bid_spread = base_spread
        ask_spread = base_spread
    
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
    
    if abs(position) < 30:
        base_volume = 80
    elif abs(position) < 60:
        base_volume = 40
    else:
        base_volume = 20
    
    buy_volume = min(base_volume, available_long)
    sell_volume = min(base_volume, available_short)
    
    return max(0, buy_volume), max(0, sell_volume)

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
    
    if urgency == "URGENT":
        if side == "bid":
            price = hedge_book.asks[0].price if hedge_book and hedge_book.asks else mid + 0.10
        else:
            price = hedge_book.bids[0].price if hedge_book and hedge_book.bids else mid - 0.10
    else:
        if side == "bid":
            price = mid + HEDGE_SPREAD
        else:
            price = mid - HEDGE_SPREAD
    
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


# -------------------------------
# Main Controller
# -------------------------------
def main():
    exchange = Exchange()
    exchange.connect()

    order_queue = Queue()
    delete_order_queue = Queue()

    prev_positions = {}
    pair_trackers = {trade_id: PairTracker() for trade_id, _ in STOCK_DUAL_PAIRS}
    news_states = {trade_id: NewsState() for trade_id, _ in STOCK_DUAL_PAIRS}
    
    # Global news state - applies to ALL stocks
    global_news_state = NewsState()
    
    loop_count = 0
    last_pnl_print = time.time()
    last_news_check = time.time()

    logging.info("=" * 80)
    logging.info("🚀 INTEGRATED CONTROLLER V2 - MASSIVE SPREAD MODE (5s HALT)")
    logging.info("=" * 80)

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

            # Track fills for P&L
            for trade_id, _ in STOCK_DUAL_PAIRS:
                old_pos = prev_positions.get(trade_id, 0)
                new_pos = positions.get(trade_id, 0)
                
                if new_pos != old_pos:
                    delta = new_pos - old_pos
                    book = order_books.get(trade_id)
                    mid = mid_from_book(book)
                    
                    if mid and delta != 0:
                        side = 'buy' if delta > 0 else 'sell'
                        pair_trackers[trade_id].record_trade(side, mid, abs(delta))

            # ===== CHECK FOR NEWS (NO ANALYSIS) =====
            if (current_time - last_news_check) >= 1.0:
                last_news_check = current_time
                
                try:
                    feeds = exchange.poll_new_social_media_feeds()
                    if feeds:
                        headline = (feeds[-1].post or "").strip()
                        if headline:
                            
                            
                            # Trigger massive spread mode for ALL stocks
                            global_news_state.trigger_news(current_time)
                            for trade_id, _ in STOCK_DUAL_PAIRS:
                                news_states[trade_id].trigger_news(current_time)
                                    
                except Exception as e:
                    logging.error(f"News processing error: {e}")

            # ===== HANDLE NEWS MODE OR NORMAL MM =====
            if global_news_state.is_active(current_time):
                # NEWS MODE: Place massive spread orders for ALL stocks (5 second halt)
                for trade_id, _ in STOCK_DUAL_PAIRS:
                    handle_news_mode(
                        exchange=exchange,
                        news_state=news_states[trade_id],
                        ticker=trade_id,
                        positions=positions,
                        outstanding_orders=outstanding_orders,
                        order_books=order_books,
                        current_time=current_time
                    )
            else:
                # Check if we just exited news mode
                if not global_news_state.active and any(ns.active for ns in news_states.values()):
                  
                    # Reset all individual news states
                    for ns in news_states.values():
                        ns.reset()
                
                # NORMAL MARKET MAKING MODE
                for trade_id, hedge_id in STOCK_DUAL_PAIRS:
                    # Cancel existing orders before placing new ones
                    cancel_all_orders(exchange, trade_id, outstanding_orders)
                    
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

                # Send orders (only in normal MM mode)
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
                            logging.error(f'🚫 [LIMIT] {order.instrument_id}: {e}')

                # Send cancels
                while not delete_order_queue.empty():
                    instr_id, oid = delete_order_queue.get()
                    try:
                        exchange.delete_order(instrument_id=instr_id, order_id=oid)
                    except:
                        pass

            # Refresh positions
            positions_now = exchange.get_positions()
            outstanding_now = {}
            for trade_id, hedge_id in STOCK_DUAL_PAIRS:
                outstanding_now[trade_id] = exchange.get_outstanding_orders(trade_id)
                outstanding_now[hedge_id] = exchange.get_outstanding_orders(hedge_id)

            # SMART HEDGING (only in normal mode, not during news)
            if not global_news_state.is_active(current_time):
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

           
            time.sleep(1)

        except KeyboardInterrupt:
            logging.info("🛑 Shutting down...")
            break
        except Exception as e:
            logging.error(f"Main loop error: {e}", exc_info=True)
            
            # Check if the error is due to a lost connection
            if "Cannot call function until connected" in str(e) or "connection" in str(e).lower():
                logging.warning("🚨 Connection lost. Attempting to reconnect...")
                
                # --- RECONNECTION LOGIC ---
                try:
                    time.sleep(1) 
                    del(exchange)
                    exchange = Exchange()
                    exchange.connect()
                    logging.info("✅ Successfully reconnected to the Exchange.")
                    
                    continue
                
                except Exception as reconnect_e:
                    logging.error(f"❌ Reconnection failed: {reconnect_e}. Retrying in 5 seconds...")
                    time.sleep(5)
            
            # For other, non-connection related errors, pause and continue
            else:
                time.sleep(1)


if __name__ == "__main__":
    main()