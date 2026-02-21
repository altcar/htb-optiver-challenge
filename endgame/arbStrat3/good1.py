"""
PROFESSIONAL MARKET MAKER - Position-Safe Implementation
========================================================
Fixed version with proper position limit enforcement
"""

import time
import logging
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Deque
import statistics
import math

from optibook.synchronous_client import Exchange

# ============================================================================
# CONFIGURATION
# ============================================================================

INSTRUMENTS = ["NVDA", "ING", "SAN", "CSCO", "PFE"]
HEDGE_PAIRS = {
    "NVDA": "NVDA_DUAL",
    "ING": "ING_DUAL",
    "SAN": "SAN_DUAL",
    "CSCO": "CSCO_DUAL",
    "PFE": "PFE_DUAL",
}

# Position limits - STRICT ENFORCEMENT
POSITION_LIMIT = 100
SAFE_POSITION_LIMIT = 90      # Start being cautious
SOFT_POSITION_LIMIT = 80      # Reduce quote sizes significantly
CRITICAL_POSITION = 70        # Only quote on reducing side
POSITION_BUFFER = 15          # Buffer for outstanding orders

# Market making parameters
BASE_SPREAD = 0.15
MIN_SPREAD = 0.08
MAX_SPREAD = 0.40
TICK_SIZE = 0.01

# Order sizing - CONSERVATIVE
BASE_QUOTE_SIZE = 40
MAX_QUOTE_SIZE = 50
MIN_QUOTE_SIZE = 10

# Inventory management
INVENTORY_DECAY = 0.4
MAX_INVENTORY_SKEW = 0.25

# Volatility and adverse selection
VOLATILITY_WINDOW = 20
MIN_EDGE = 0.05

# Smart hedging
HEDGE_THRESHOLD = 20
URGENT_HEDGE = 50
HEDGE_SIZE = 15

# Performance
LOOP_INTERVAL = 0.3
ORDER_REFRESH_INTERVAL = 0.6
RISK_CHECK_INTERVAL = 1.0

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logging.getLogger("client").setLevel("ERROR")
logger = logging.getLogger(__name__)

# ============================================================================
# POSITION LIMIT CHECKER
# ============================================================================

class PositionLimitChecker:
    """Strict position limit enforcement"""
    
    @staticmethod
    def get_safe_volume(instrument: str, side: str, desired_volume: int, 
                       current_position: int, outstanding_orders: dict) -> int:
        """Calculate maximum safe volume that won't breach limits"""
        
        # Calculate outstanding volume on each side
        outstanding_buy = sum(
            order.volume for order in outstanding_orders.values() 
            if order.side == 'bid'
        )
        outstanding_sell = sum(
            order.volume for order in outstanding_orders.values() 
            if order.side == 'ask'
        )
        
        if side == 'bid':
            # Worst case: all outstanding buys fill, all outstanding sells cancel
            worst_case_position = current_position + outstanding_buy
            # Maximum we can buy
            max_buy = POSITION_LIMIT - POSITION_BUFFER - worst_case_position
            safe_volume = max(0, min(desired_volume, max_buy))
        else:  # ask
            # Worst case: all outstanding sells fill, all outstanding buys cancel
            worst_case_position = current_position - outstanding_sell
            # Maximum we can sell
            max_sell = POSITION_LIMIT - POSITION_BUFFER + worst_case_position
            safe_volume = max(0, min(desired_volume, max_sell))
        
        return int(safe_volume)
    
    @staticmethod
    def can_trade(instrument: str, side: str, volume: int,
                  current_position: int, outstanding_orders: dict) -> Tuple[bool, str]:
        """Check if trade is safe"""
        
        safe_volume = PositionLimitChecker.get_safe_volume(
            instrument, side, volume, current_position, outstanding_orders
        )
        
        if safe_volume == 0:
            return False, f"Position limit: pos={current_position}, limit={POSITION_LIMIT}"
        
        if safe_volume < volume:
            return False, f"Reduced volume from {volume} to {safe_volume}"
        
        return True, "OK"

# ============================================================================
# DATA STRUCTURES
# ============================================================================

@dataclass
class MarketData:
    """Real-time market data"""
    bid: float
    ask: float
    bid_size: int
    ask_size: int
    mid: float
    spread: float
    timestamp: float
    
    @property
    def is_crossed(self) -> bool:
        return self.bid >= self.ask
    
    @property
    def is_locked(self) -> bool:
        return self.bid == self.ask

@dataclass
class Quote:
    """Our quote state"""
    bid_price: float
    ask_price: float
    bid_size: int
    ask_size: int
    bid_order_id: Optional[int] = None
    ask_order_id: Optional[int] = None
    timestamp: float = 0.0

@dataclass
class Trade:
    """Trade record"""
    price: float
    size: int
    side: str
    timestamp: float

# ============================================================================
# ANALYTICS ENGINE
# ============================================================================

class MarketAnalytics:
    """Advanced market analytics"""
    
    def __init__(self, window_size: int = 20):
        self.window_size = window_size
        self.price_history: Deque[float] = deque(maxlen=window_size)
        self.trade_history: Deque[Trade] = deque(maxlen=100)
        self.spread_history: Deque[float] = deque(maxlen=window_size)
        
    def update(self, market_data: MarketData):
        self.price_history.append(market_data.mid)
        self.spread_history.append(market_data.spread)
    
    def record_trade(self, trade: Trade):
        self.trade_history.append(trade)
    
    @property
    def volatility(self) -> float:
        if len(self.price_history) < 2:
            return 0.01
        
        returns = []
        for i in range(1, len(self.price_history)):
            ret = (self.price_history[i] - self.price_history[i-1]) / self.price_history[i-1]
            returns.append(ret)
        
        if not returns:
            return 0.01
        
        return statistics.stdev(returns) if len(returns) > 1 else 0.01
    
    @property
    def avg_spread(self) -> float:
        if not self.spread_history:
            return BASE_SPREAD
        return statistics.mean(self.spread_history)
    
    def estimate_adverse_selection_risk(self, position: int) -> float:
        if len(self.trade_history) < 5:
            return 0.0
        
        recent_trades = list(self.trade_history)[-5:]
        buy_prices = [t.price for t in recent_trades if t.side == 'buy']
        sell_prices = [t.price for t in recent_trades if t.side == 'sell']
        
        if len(buy_prices) >= 3:
            if all(buy_prices[i] <= buy_prices[i+1] for i in range(len(buy_prices)-1)):
                return 0.05
        
        if len(sell_prices) >= 3:
            if all(sell_prices[i] >= sell_prices[i+1] for i in range(len(sell_prices)-1)):
                return 0.05
        
        return 0.0

# ============================================================================
# PRICING ENGINE
# ============================================================================

class PricingEngine:
    """Professional pricing with strict position awareness"""
    
    def __init__(self):
        self.analytics = {}
        
    def get_analytics(self, instrument: str) -> MarketAnalytics:
        if instrument not in self.analytics:
            self.analytics[instrument] = MarketAnalytics()
        return self.analytics[instrument]
    
    def calculate_spread(self, instrument: str, market_data: MarketData, position: int) -> float:
        analytics = self.get_analytics(instrument)
        
        market_spread = market_data.spread
        base_spread = max(BASE_SPREAD, market_spread * 0.6)
        
        # Volatility adjustment
        vol = analytics.volatility
        vol_multiplier = 1.0 + (vol * 50)
        
        # Position adjustment - MORE AGGRESSIVE
        pos_pct = abs(position) / POSITION_LIMIT
        if abs(position) >= SOFT_POSITION_LIMIT:
            pos_multiplier = 1.5 + (pos_pct * 1.0)  # Much wider spreads
        else:
            pos_multiplier = 1.0 + (pos_pct * 0.3)
        
        # Adverse selection
        adverse_risk = analytics.estimate_adverse_selection_risk(position)
        
        spread = base_spread * vol_multiplier * pos_multiplier + adverse_risk
        spread = max(MIN_SPREAD, min(MAX_SPREAD, spread))
        
        return spread
    
    def calculate_inventory_skew(self, position: int, position_limit: int) -> float:
        if position == 0:
            return 0.0
        
        norm_pos = position / position_limit
        skew = -norm_pos * abs(norm_pos) * INVENTORY_DECAY
        skew = max(-MAX_INVENTORY_SKEW, min(MAX_INVENTORY_SKEW, skew))
        
        return skew
    
    def calculate_quote_size(self, position: int, side: str, outstanding_orders: dict) -> int:
        """Calculate safe quote size with position limits"""
        abs_pos = abs(position)
        
        # CRITICAL: Only quote on reducing side
        if abs_pos >= CRITICAL_POSITION:
            if (position > 0 and side == 'ask') or (position < 0 and side == 'bid'):
                return MAX_QUOTE_SIZE  # Reduce position aggressively
            else:
                return 0  # Don't add to position
        
        # SOFT LIMIT: Reduce adding side significantly
        if abs_pos >= SOFT_POSITION_LIMIT:
            if (position > 0 and side == 'bid') or (position < 0 and side == 'ask'):
                return MIN_QUOTE_SIZE  # Minimal adding
            else:
                return BASE_QUOTE_SIZE  # Normal reducing
        
        # SAFE LIMIT: Start being cautious
        if abs_pos >= SAFE_POSITION_LIMIT:
            if (position > 0 and side == 'bid') or (position < 0 and side == 'ask'):
                return MIN_QUOTE_SIZE * 2
            else:
                return BASE_QUOTE_SIZE
        
        # Normal quoting
        return BASE_QUOTE_SIZE
    
    def calculate_quote(self, instrument: str, market_data: MarketData, 
                       position: int, outstanding_orders: dict) -> Optional[Quote]:
        """Calculate optimal two-sided quote with position safety"""
        analytics = self.get_analytics(instrument)
        analytics.update(market_data)
        
        if market_data.is_crossed or market_data.is_locked:
            return None
        
        # Calculate spread and skew
        spread = self.calculate_spread(instrument, market_data, position)
        half_spread = spread / 2
        skew = self.calculate_inventory_skew(position, POSITION_LIMIT)
        
        # Calculate prices
        theo_mid = market_data.mid + skew
        bid_price = theo_mid - half_spread
        ask_price = theo_mid + half_spread
        
        # Improve market by 1 tick
        bid_price = min(bid_price, market_data.bid - TICK_SIZE)
        ask_price = max(ask_price, market_data.ask + TICK_SIZE)
        
        # Round to tick
        bid_price = round(bid_price / TICK_SIZE) * TICK_SIZE
        ask_price = round(ask_price / TICK_SIZE) * TICK_SIZE
        
        # Verify minimum edge
        if (market_data.mid - bid_price) < MIN_EDGE or (ask_price - market_data.mid) < MIN_EDGE:
            return None
        
        # Calculate desired sizes
        bid_size = self.calculate_quote_size(position, 'bid', outstanding_orders)
        ask_size = self.calculate_quote_size(position, 'ask', outstanding_orders)
        
        # CRITICAL: Enforce position limits
        bid_size = PositionLimitChecker.get_safe_volume(
            instrument, 'bid', bid_size, position, outstanding_orders
        )
        ask_size = PositionLimitChecker.get_safe_volume(
            instrument, 'ask', ask_size, position, outstanding_orders
        )
        
        if bid_size == 0 and ask_size == 0:
            logger.warning(f"⚠️  {instrument}: No safe quote size (pos={position})")
            return None
        
        return Quote(
            bid_price=bid_price,
            ask_price=ask_price,
            bid_size=bid_size,
            ask_size=ask_size,
            timestamp=time.time()
        )

# ============================================================================
# ORDER MANAGER
# ============================================================================

class OrderManager:
    """Safe order management with position limit enforcement"""
    
    def __init__(self, exchange: Exchange):
        self.exchange = exchange
        self.active_quotes: Dict[str, Quote] = {}
        self.last_refresh: Dict[str, float] = {}
    
    def get_market_data(self, instrument: str) -> Optional[MarketData]:
        try:
            book = self.exchange.get_last_price_book(instrument)
            if not book or not book.bids or not book.asks:
                return None
            
            return MarketData(
                bid=book.bids[0].price,
                ask=book.asks[0].price,
                bid_size=book.bids[0].volume,
                ask_size=book.asks[0].volume,
                mid=(book.bids[0].price + book.asks[0].price) / 2,
                spread=book.asks[0].price - book.bids[0].price,
                timestamp=time.time()
            )
        except:
            return None
    
    def should_refresh_quotes(self, instrument: str, new_quote: Quote) -> bool:
        current_time = time.time()
        
        if instrument not in self.last_refresh:
            return True
        
        if current_time - self.last_refresh[instrument] >= ORDER_REFRESH_INTERVAL:
            return True
        
        if instrument not in self.active_quotes:
            return True
        
        old_quote = self.active_quotes[instrument]
        
        if abs(new_quote.bid_price - old_quote.bid_price) > TICK_SIZE * 2:
            return True
        if abs(new_quote.ask_price - old_quote.ask_price) > TICK_SIZE * 2:
            return True
        
        if abs(new_quote.bid_size - old_quote.bid_size) > 15:
            return True
        if abs(new_quote.ask_size - old_quote.ask_size) > 15:
            return True
        
        return False
    
    def cancel_all_orders(self, instrument: str):
        """Cancel all outstanding orders"""
        try:
            outstanding = self.exchange.get_outstanding_orders(instrument)
            for order_id in outstanding.keys():
                try:
                    self.exchange.delete_order(instrument, order_id)
                except:
                    pass
        except:
            pass
    
    def update_quotes(self, instrument: str, quote: Quote, position: int, outstanding_orders: dict):
        """Update quotes with position safety checks"""
        try:
            # Cancel existing orders first
            self.cancel_all_orders(instrument)
            time.sleep(0.02)  # Brief pause for cancellations to process
            
            # Refresh outstanding orders after cancellation
            outstanding_orders = self.exchange.get_outstanding_orders(instrument)
            
            # Place bid order with safety check
            if quote.bid_size > 0:
                can_trade, reason = PositionLimitChecker.can_trade(
                    instrument, 'bid', quote.bid_size, position, outstanding_orders
                )
                
                if can_trade:
                    try:
                        self.exchange.insert_order(
                            instrument_id=instrument,
                            price=quote.bid_price,
                            volume=quote.bid_size,
                            side='bid',
                            order_type='limit'
                        )
                    except Exception as e:
                        if 'position limit' in str(e).lower():
                            logger.error(f"🚫 {instrument} BID: {e}")
                        else:
                            logger.error(f"Bid order error {instrument}: {e}")
                else:
                    logger.debug(f"{instrument} BID blocked: {reason}")
            
            # Place ask order with safety check
            if quote.ask_size > 0:
                can_trade, reason = PositionLimitChecker.can_trade(
                    instrument, 'ask', quote.ask_size, position, outstanding_orders
                )
                
                if can_trade:
                    try:
                        self.exchange.insert_order(
                            instrument_id=instrument,
                            price=quote.ask_price,
                            volume=quote.ask_size,
                            side='ask',
                            order_type='limit'
                        )
                    except Exception as e:
                        if 'position limit' in str(e).lower():
                            logger.error(f"🚫 {instrument} ASK: {e}")
                        else:
                            logger.error(f"Ask order error {instrument}: {e}")
                else:
                    logger.debug(f"{instrument} ASK blocked: {reason}")
            
            self.active_quotes[instrument] = quote
            self.last_refresh[instrument] = time.time()
            
        except Exception as e:
            logger.error(f"Update quotes error {instrument}: {e}")

# ============================================================================
# HEDGE MANAGER
# ============================================================================

class HedgeManager:
    """Smart delta hedging with position safety"""
    
    def __init__(self, exchange: Exchange):
        self.exchange = exchange
        self.last_hedge: Dict[str, float] = {}
    
    def calculate_net_position(self, instrument: str, hedge_instrument: str) -> int:
        try:
            positions = self.exchange.get_positions()
            main_pos = positions.get(instrument, 0)
            hedge_pos = positions.get(hedge_instrument, 0)
            return main_pos + hedge_pos
        except:
            return 0
    
    def should_hedge(self, instrument: str, net_position: int) -> Tuple[bool, str]:
        abs_net = abs(net_position)
        
        if abs_net >= URGENT_HEDGE:
            return True, "URGENT"
        elif abs_net >= HEDGE_THRESHOLD:
            if instrument in self.last_hedge:
                if time.time() - self.last_hedge[instrument] < 3.0:
                    return False, "COOLDOWN"
            return True, "NORMAL"
        
        return False, "NONE"
    
    def execute_hedge(self, hedge_instrument: str, net_position: int, urgency: str) -> bool:
        try:
            # Get current hedge position
            positions = self.exchange.get_positions()
            hedge_pos = positions.get(hedge_instrument, 0)
            outstanding = self.exchange.get_outstanding_orders(hedge_instrument)
            
            book = self.exchange.get_last_price_book(hedge_instrument)
            if not book or not book.bids or not book.asks:
                return False
            
            # Determine hedge size
            if urgency == "URGENT":
                hedge_size = min(abs(net_position), HEDGE_SIZE * 2)
            else:
                hedge_size = min(HEDGE_SIZE, abs(net_position))
            
            if hedge_size < 5:
                return False
            
            # Determine side
            side = 'bid' if net_position < 0 else 'ask'
            
            # Check position limits on hedge instrument
            can_trade, reason = PositionLimitChecker.can_trade(
                hedge_instrument, side, hedge_size, hedge_pos, outstanding
            )
            
            if not can_trade:
                logger.warning(f"⚠️  Hedge blocked for {hedge_instrument}: {reason}")
                return False
            
            # Aggressive pricing
            if side == 'bid':
                price = book.asks[0].price + TICK_SIZE
            else:
                price = book.bids[0].price - TICK_SIZE
            
            self.exchange.insert_order(
                instrument_id=hedge_instrument,
                price=price,
                volume=int(hedge_size),
                side=side,
                order_type='ioc'
            )
            
            logger.info(f"🛡️  HEDGE-{urgency}: {side.upper()} {hedge_instrument} {hedge_size} @ {price:.2f}")
            return True
            
        except Exception as e:
            logger.error(f"Hedge error: {e}")
            return False

# ============================================================================
# MAIN MARKET MAKER
# ============================================================================

class ProfessionalMarketMaker:
    """Professional market making system with strict position safety"""
    
    def __init__(self):
        self.exchange = Exchange()
        self.exchange.connect()
        
        self.pricing_engine = PricingEngine()
        self.order_manager = OrderManager(self.exchange)
        self.hedge_manager = HedgeManager(self.exchange)
        
        self.running = True
        self.last_risk_check = 0
        self.last_status_print = 0
        
        self.pnl_tracker = defaultdict(lambda: {'buys': [], 'sells': []})
        
        logger.info("=" * 80)
        logger.info("🏦 PROFESSIONAL MARKET MAKER - POSITION-SAFE VERSION")
        logger.info("=" * 80)
    
    def update_pnl_tracking(self, instrument: str, old_pos: int, new_pos: int, market_data: MarketData):
        delta = new_pos - old_pos
        if delta == 0:
            return
        
        price = market_data.mid
        side = 'buy' if delta > 0 else 'sell'
        
        self.pnl_tracker[instrument][side + 's'].append((price, abs(delta)))
        
        analytics = self.pricing_engine.get_analytics(instrument)
        analytics.record_trade(Trade(
            price=price,
            size=abs(delta),
            side=side,
            timestamp=time.time()
        ))
    
    def calculate_pnl(self) -> Dict[str, float]:
        pnls = {}
        
        for instrument in INSTRUMENTS:
            buys = self.pnl_tracker[instrument]['buys']
            sells = self.pnl_tracker[instrument]['sells']
            
            buy_queue = []
            for price, vol in buys:
                buy_queue.extend([price] * int(vol))
            
            sell_queue = []
            for price, vol in sells:
                sell_queue.extend([price] * int(vol))
            
            pnl = 0.0
            matched = min(len(buy_queue), len(sell_queue))
            for i in range(matched):
                pnl += (sell_queue[i] - buy_queue[i])
            
            pnls[instrument] = pnl
        
        return pnls
    
    def print_status(self, positions: Dict[str, int]):
        current_time = time.time()
        if current_time - self.last_status_print < 5.0:
            return
        
        self.last_status_print = current_time
        
        pnls = self.calculate_pnl()
        total_pnl = sum(pnls.values())
        
        logger.info("=" * 80)
        logger.info(f"📊 STATUS | Total P&L: ${total_pnl:+,.2f}")
        logger.info("-" * 80)
        
        for instrument in INSTRUMENTS:
            pos = positions.get(instrument, 0)
            pnl = pnls.get(instrument, 0.0)
            
            # Get outstanding info
            try:
                outstanding = self.exchange.get_outstanding_orders(instrument)
                out_buy = sum(o.volume for o in outstanding.values() if o.side == 'bid')
                out_sell = sum(o.volume for o in outstanding.values() if o.side == 'ask')
                out_str = f"Out:[B{out_buy} S{out_sell}]"
            except:
                out_str = "Out:[N/A]"
            
            pos_pct = abs(pos) / POSITION_LIMIT * 100
            if abs(pos) >= CRITICAL_POSITION:
                pos_status = "🔴 CRITICAL"
            elif abs(pos) >= SOFT_POSITION_LIMIT:
                pos_status = "🟡 HIGH"
            elif abs(pos) >= SAFE_POSITION_LIMIT:
                pos_status = "🟠 CAUTION"
            else:
                pos_status = "🟢 OK"
            
            quote = self.order_manager.active_quotes.get(instrument)
            if quote:
                spread = quote.ask_price - quote.bid_price
                quote_str = f"[{quote.bid_price:.2f}x{quote.bid_size} / {quote.ask_size}x{quote.ask_price:.2f}] Δ={spread:.3f}"
            else:
                quote_str = "[NO QUOTE]"
            
            logger.info(
                f"  {instrument:4s}: Pos={pos:+4d} ({pos_pct:5.1f}%) {pos_status} {out_str} | "
                f"P&L=${pnl:+7.2f} | {quote_str}"
            )
        
        logger.info("=" * 80)
    
    def run_risk_checks(self, positions: Dict[str, int]):
        current_time = time.time()
        if current_time - self.last_risk_check < RISK_CHECK_INTERVAL:
            return
        
        self.last_risk_check = current_time
        
        for instrument in INSTRUMENTS:
            pos = positions.get(instrument, 0)
            
            if abs(pos) >= SAFE_POSITION_LIMIT:
                logger.warning(f"⚠️  {instrument}: High position {pos}/{POSITION_LIMIT}")
            
            if abs(pos) >= CRITICAL_POSITION:
                logger.error(f"🚨 {instrument}: CRITICAL position {pos}/{POSITION_LIMIT} - REDUCING ONLY")
    
    def run(self):
        logger.info("🚀 Starting market making with strict position control...")
        
        prev_positions = {}
        
        while self.running:
            try:
                loop_start = time.time()
                
                # Get positions
                positions = self.exchange.get_positions()
                
                # Market make on all instruments
                for instrument in INSTRUMENTS:
                    market_data = self.order_manager.get_market_data(instrument)
                    if not market_data:
                        continue
                    
                    # Track P&L
                    old_pos = prev_positions.get(instrument, 0)
                    new_pos = positions.get(instrument, 0)
                    if old_pos != new_pos:
                        self.update_pnl_tracking(instrument, old_pos, new_pos, market_data)
                    
                    # Get outstanding orders
                    outstanding_orders = self.exchange.get_outstanding_orders(instrument)
                    
                    # Calculate quote
                    position = positions.get(instrument, 0)
                    quote = self.pricing_engine.calculate_quote(
                        instrument, market_data, position, outstanding_orders
                    )
                    
                    if quote:
                        if self.order_manager.should_refresh_quotes(instrument, quote):
                            self.order_manager.update_quotes(
                                instrument, quote, position, outstanding_orders
                            )
                    else:
                        self.order_manager.cancel_all_orders(instrument)
                
                # Execute hedges
                for instrument, hedge_instrument in HEDGE_PAIRS.items():
                    net_pos = self.hedge_manager.calculate_net_position(instrument, hedge_instrument)
                    should_hedge, urgency = self.hedge_manager.should_hedge(instrument, net_pos)
                    
                    if should_hedge:
                        if self.hedge_manager.execute_hedge(hedge_instrument, net_pos, urgency):
                            self.hedge_manager.last_hedge[instrument] = time.time()
                
                # Risk management
                self.run_risk_checks(positions)
                
                # Status updates
                self.print_status(positions)
                
                prev_positions = positions.copy()
                
                # Sleep
                elapsed = time.time() - loop_start
                sleep_time = max(0, LOOP_INTERVAL - elapsed)
                time.sleep(sleep_time)
                
            except KeyboardInterrupt:
                logger.info("🛑 Shutting down...")
                self.running = False
                break
                
            except Exception as e:
                logger.error(f"Main loop error: {e}", exc_info=True)
                
                if "connection" in str(e).lower():
                    logger.warning("🚨 Reconnecting...")
                    try:
                        time.sleep(1)
                        self.exchange = Exchange()
                        self.exchange.connect()
                        self.order_manager = OrderManager(self.exchange)
                        self.hedge_manager = HedgeManager(self.exchange)
                        logger.info("✅ Reconnected")
                    except:
                        logger.error("❌ Reconnection failed")
                        time.sleep(5)
                else:
                    time.sleep(1)
        
        # Clean shutdown
        logger.info("🧹 Cancelling all orders...")
        for instrument in INSTRUMENTS:
            self.order_manager.cancel_all_orders(instrument)
            if instrument in HEDGE_PAIRS:
                self.order_manager.cancel_all_orders(HEDGE_PAIRS[instrument])
        
        logger.info("=" * 80)
        logger.info("🏁 SHUTDOWN COMPLETE")
        logger.info("=" * 80)

# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    mm = ProfessionalMarketMaker()
    mm.run()

if __name__ == "__main__":
    main()