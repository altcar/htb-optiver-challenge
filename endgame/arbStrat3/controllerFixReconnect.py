# integrated_controller.py
import time
import logging
from queue import Queue
from collections import defaultdict, deque, namedtuple
from math import exp
import numpy as np
import torch

from optibook.synchronous_client import Exchange
from transformers import pipeline

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
# Sentiment model
# -------------------------------
MODEL_NAME = "distilbert-base-uncased-finetuned-sst-2-english"
device = 0 if torch.cuda.is_available() else -1

try:
    sentiment_pipeline = pipeline(
        "sentiment-analysis",
        model=MODEL_NAME,
        tokenizer=MODEL_NAME,
        device=device,
        truncation=True
    )
    SENTIMENT_ENABLED = True
    logging.info("Sentiment analysis enabled")
except Exception as e:
    logging.warning(f"Sentiment analysis disabled: {e}")
    SENTIMENT_ENABLED = False

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
MIN_PROFITABLE_SPREAD = 0.05
MAX_SPREAD = 2
HEDGE_BATCH_SIZE = 20
URGENT_HEDGE_THRESHOLD = 70
HEDGE_SPREAD = 0.03
POSITION_BUFFER = 10

# Sentiment Trading Parameters
SENTIMENT_POSITION_ALLOCATION = 30  # Max position from sentiment trades
SENTIMENT_THRESHOLD = 0.05
ROLLING_WINDOW = 20
MAX_KELLY_FRACTION = 0.3  # More conservative
SENTIMENT_TRADE_SIZE = 15  # Fixed size per sentiment trade
MIN_SENTIMENT_INTERVAL = 5.0  # Seconds between sentiment trades per ticker

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


class SentimentTracker:
    """Track sentiment signals and trades"""
    def __init__(self):
        self.score_history = deque(maxlen=ROLLING_WINDOW)
        self.last_trade_time = 0
        self.trade_history = deque(maxlen=50)
        self.sentiment_position = 0
        
    def add_score(self, score):
        """Add new sentiment score"""
        self.score_history.append(score)
        
    def get_z_score(self):
        """Calculate z-score of current sentiment"""
        if len(self.score_history) < 2:
            return 0.0
        current = self.score_history[-1]
        mean = np.mean(self.score_history)
        std = np.std(self.score_history)
        if std == 0:
            return 0.0
        return (current - mean) / std
    
    def can_trade(self, current_time):
        """Check if enough time has passed since last trade"""
        return (current_time - self.last_trade_time) >= MIN_SENTIMENT_INTERVAL
    
    def record_trade(self, volume, timestamp):
        """Record a sentiment trade"""
        self.last_trade_time = timestamp
        self.trade_history.append((volume, timestamp))
        self.sentiment_position += volume


# -------------------------------
# Sentiment Functions
# -------------------------------
def sentiment_to_score(headline: str) -> float:
    """Convert headline to sentiment score"""
    if not headline or not SENTIMENT_ENABLED:
        return 0.0
    try:
        res = sentiment_pipeline(headline, top_k=None)[0]
        score = res['score'] * 100
        return score if res['label'] == "POSITIVE" else -score
    except Exception as e:
        logging.error(f"Sentiment analysis error: {e}")
        return 0.0


def calculate_sentiment_position(z_score, current_pos, position_limit, sentiment_allocation):
    """
    Calculate target position based on sentiment
    Returns the delta to trade (positive = buy, negative = sell)
    """
    if abs(z_score) < SENTIMENT_THRESHOLD:
        return 0
    
    # Calculate target position using Kelly criterion (conservative)
    p = np.clip(1 / (1 + exp(-z_score)), 0.2, 0.8)
    kelly_fraction = np.clip((p - (1 - p)), -MAX_KELLY_FRACTION, MAX_KELLY_FRACTION)
    target_sentiment_pos = int(kelly_fraction * sentiment_allocation)
    
    # Calculate how much we'd need to trade
    # But limit to fixed trade size to avoid large moves
    delta = target_sentiment_pos - current_pos
    
    if abs(delta) < 5:  # Minimum meaningful trade
        return 0
    
    # Limit trade size
    if delta > 0:
        return min(delta, SENTIMENT_TRADE_SIZE)
    else:
        return max(delta, -SENTIMENT_TRADE_SIZE)


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
    position_ratio = position / position_limit
    
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


def calculate_optimal_volume(position, outstanding_orders, position_limit, sentiment_allocation):
    """
    Calculate optimal volume for MM, reserving space for sentiment trades
    """
    # Reserve space for sentiment trading
    effective_limit = position_limit - sentiment_allocation
    
    available_long = effective_limit - POSITION_BUFFER - position
    available_short = effective_limit - POSITION_BUFFER + position
    
    outstanding_buy = sum(o.volume for o in outstanding_orders.values() if o.side == 'bid')
    outstanding_sell = sum(o.volume for o in outstanding_orders.values() if o.side == 'ask')
    
    available_long = max(0, available_long - outstanding_buy)
    available_short = max(0, available_short - outstanding_sell)
    
    if abs(position) < 30:
        base_volume = 60  # Reduced from 80 to leave room for sentiment
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
        # FIX: Convert numpy types to Python int
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


def execute_sentiment_trade(exchange, ticker, trade_volume, book, current_position, outstanding_orders):
    """
    Execute a sentiment-based trade
    FIX: Properly convert all numeric types to Python natives
    """
    if trade_volume == 0:
        return False
    
    if not book or not book.bids or not book.asks:
        return False
    
    side = "bid" if trade_volume > 0 else "ask"
    volume = abs(int(trade_volume))  # FIX: Convert to Python int
    
    # Check position limits
    if side == "bid":
        outstanding_buy = sum(o.volume for o in outstanding_orders.values() if o.side == 'bid')
        max_safe = POSITION_LIMIT - POSITION_BUFFER - current_position - outstanding_buy
    else:
        outstanding_sell = sum(o.volume for o in outstanding_orders.values() if o.side == 'ask')
        max_safe = POSITION_LIMIT - POSITION_BUFFER + current_position - outstanding_sell
    
    if max_safe < volume:
        volume = max(0, int(max_safe))  # FIX: Convert to Python int
    
    if volume <= 0:
        return False
    
    # Get aggressive price to ensure fill
    if side == "bid":
        price = float(book.asks[0].price)  # FIX: Convert to Python float
    else:
        price = float(book.bids[0].price)  # FIX: Convert to Python float
    
    try:
        oid = exchange.insert_order(
            instrument_id=ticker,
            price=price,
            volume=volume,
            side=side,
            order_type="ioc",
        )
        logging.info(f'💡 [SENTIMENT] {side} {ticker} {volume} @ {price:.2f}')
        return True
    except Exception as e:
        logging.error(f'❌ [SENTIMENT-TRADE] {ticker}: {e}')
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
    sentiment_trackers = {trade_id: SentimentTracker() for trade_id, _ in STOCK_DUAL_PAIRS}
    
    loop_count = 0
    last_pnl_print = time.time()
    last_sentiment_check = time.time()

    logging.info("🚀 Integrated MM + Sentiment Controller Started")

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

            # SENTIMENT TRADING (every 2 seconds)
            if SENTIMENT_ENABLED and (current_time - last_sentiment_check) >= 2.0:
                last_sentiment_check = current_time
                
                try:
                    feeds = exchange.poll_new_social_media_feeds()
                    if feeds:
                        headline = (feeds[-1].post or "").strip()
                        if headline:
                            score = sentiment_to_score(headline)
                            logging.info(f"📰 Headline: {headline[:80]}... | Score: {score:.1f}")
                            
                            # Apply sentiment to all tickers
                            for trade_id, _ in STOCK_DUAL_PAIRS:
                                tracker = sentiment_trackers[trade_id]
                                tracker.add_score(score)
                                
                                if tracker.can_trade(current_time):
                                    z_score = tracker.get_z_score()
                                    current_pos = positions.get(trade_id, 0)
                                    
                                    trade_volume = calculate_sentiment_position(
                                        z_score=z_score,
                                        current_pos=tracker.sentiment_position,
                                        position_limit=POSITION_LIMIT,
                                        sentiment_allocation=SENTIMENT_POSITION_ALLOCATION
                                    )
                                    
                                    if trade_volume != 0:
                                        book = order_books.get(trade_id)
                                        outs = outstanding_orders.get(trade_id, {})
                                        
                                        success = execute_sentiment_trade(
                                            exchange=exchange,
                                            ticker=trade_id,
                                            trade_volume=trade_volume,
                                            book=book,
                                            current_position=current_pos,
                                            outstanding_orders=outs
                                        )
                                        
                                        if success:
                                            tracker.record_trade(trade_volume, current_time)
                                            
                except Exception as e:
                    logging.error(f"Sentiment processing error: {e}")

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
                    position_limit=POSITION_LIMIT,
                    sentiment_allocation=SENTIMENT_POSITION_ALLOCATION
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


            # Send orders
            while not order_queue.empty():
                order = order_queue.get()

                # check wheter there will be a self-trade to avoid being disconnected

                # get order id
                # get_outstandingf_order(order id)

                # print("jajfdjsa ===================")
                # instrument_id = order.instrument_id
                # price = order.price; volume = order.volume
                # side = order.side; order_type = order.order_type
                # print(f'instrument id: {instrument_id}')
                # outstanding_orders = exchange.get_outstanding_orders(instrument_id)

                # oposite_side = "ask" if side == "bid" else "bid"

                # print(f'Side: {side} | Opposite Side: {oposite_side} | ')

                # orders_against = [order_status for order_status in outstanding_orders.values() if order_status.side == oposite_side]

                # print(orders_against)

                # for order_against in orders_against:
                #     if side == "bid":
                #         if price > order_against.price:
                #             print("CANCELLL ORDER NOW")
                #     if side == "ask":
                #         if price < order_against.price:
                #             print("CANCELLL THE ORDER") 


                
                instrument_outs = outstanding_orders.get(order.instrument_id, {})
                if check_breach_position_limit(order, positions, instrument_outs, POSITION_LIMIT):
                    continue
                
                try:
                    # FIX: Ensure all order parameters are Python native types
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

            # SMART HEDGING
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

            # P&L reporting
            if current_time - last_pnl_print > 10:
#                If we want to get rif of printing errors then we can use this ocddl
                last_pnl_print = current_time
                
                total_pnl = sum(tracker.get_pnl() for tracker in pair_trackers.values())
                
                logging.info("=" * 80)
                logging.info(f"📊 PnL: {total_pnl:.2f} | Positions: {dict(positions_now)}")
                
                for trade_id, _ in STOCK_DUAL_PAIRS:
                    pos = positions_now.get(trade_id, 0)
                    pnl = pair_trackers[trade_id].get_pnl()
                    sent_pos = sentiment_trackers[trade_id].sentiment_position
                    logging.info(f"  {trade_id}: Pos={pos:+4d} | PnL={pnl:+8.2f} | Sentiment={sent_pos:+3d}")
                
                logging.info("=" * 80)

            
            time.sleep(.2)

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
                    # Attempt to re-connect using the existing exchange object
                    time.sleep(0.5) 
                    del(exchange)
                    exchange = Exchange()
                    exchange.connect()
                    logging.info("✅ Successfully reconnected to the Exchange.")
                    # Sleep briefly before resuming trading loop
                    
                    continue # Jumps back to the start of the while loop
                
                except Exception as reconnect_e:
                    logging.error(f"❌ Reconnection failed: {reconnect_e}. Retrying in 5 seconds...")
                    time.sleep(5)
                    # If reconnection fails, the loop continues to the next iteration
                    # and will attempt to reconnect again if the error persists.
            
            # For other, non-connection related errors, pause and continue
            else:
                time.sleep(1)




if __name__ == "__main__":
    main()