"""
PROFESSIONAL MARKET MAKER - Improved Competitive Version
=========================================================
Optimized for 30-participant competitive environment
with proper position management and aggressive market making
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

# Position limits - FIXED THRESHOLDS
POSITION_LIMIT = 100
SAFE_POSITION_LIMIT = 60       # Start being cautious at 60%
SOFT_POSITION_LIMIT = 80       # Reduce significantly at 80%
CRITICAL_POSITION = 90         # Emergency mode at 90%
POSITION_BUFFER = 5            # Smaller buffer for more aggressive trading

# Market making parameters - MORE COMPETITIVE
BASE_SPREAD = 0.03
MIN_SPREAD = 0.02              # Tighter minimum spread
MAX_SPREAD = 2.0               # Lower ceiling
TICK_SIZE = 0.01

# Order sizing - DYNAMIC
BASE_QUOTE_SIZE = 20
MAX_QUOTE_SIZE = 50
MIN_QUOTE_SIZE = 5

# Inventory management - MORE AGGRESSIVE
INVENTORY_DECAY = 0.8          # More aggressive skewing
MAX_INVENTORY_SKEW = 0.50      # Allow larger skews to reduce inventory

# Volatility and adverse selection
VOLATILITY_WINDOW = 20
MIN_EDGE = 0.03                # Reduced minimum edge

# Smart hedging - MORE AGGRESSIVE
HEDGE_THRESHOLD = 30           # Start hedging earlier
URGENT_HEDGE = 60              # Urgent at 60% of limit
HEDGE_SIZE = 20                # Larger hedge sizes
HEDGE_AGGRESSIVE_RATIO = 0.7   # 70% aggressive, 30% passive

# Performance - FASTER
LOOP_INTERVAL = 0.05           # 50ms for faster reactions
ORDER_REFRESH_INTERVAL = 0.3   # Refresh more frequently
MIN_REFRESH_INTERVAL = 0.15    # Minimum time between refreshes
RISK_CHECK_INTERVAL = 1.0
STALENESS_THRESHOLD = 0.3      # Consider quotes stale after 300ms

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
        self.last_mid = None
        
    def update(self, market_data: MarketData):
        self.price_history.append(market_data.mid)
        self.spread_history.append(market_data.spread)
        self.last_mid = market_data.mid
    
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
    
    @property
    def price_momentum(self) -> float:
        """Calculate recent price momentum"""
        if len(self.price_history) < 5:
            return 0.0
        
        recent = list(self.price_history)[-5:]
        if recent[-1] > recent[0]:
            return (recent[-1] - recent[0]) / recent[0]
        else:
            return (recent[-1] - recent[0]) / recent[0]
    
    def estimate_adverse_selection_risk(self, position: int) -> float:
        if len(self.trade_history) < 5:
            return 0.0
        
        recent_trades = list(self.trade_history)[-5:]
        buy_prices = [t.price for t in recent_trades if t.side == 'buy']
        sell_prices = [t.price for t in recent_trades if t.side == 'sell']
        
        # Detect if we're being picked off
        if len(buy_prices) >= 3:
            if all(buy_prices[i] <= buy_prices[i+1] for i in range(len(buy_prices)-1)):
                return 0.03  # Reduced penalty
        
        if len(sell_prices) >= 3:
            if all(sell_prices[i] >= sell_prices[i+1] for i in range(len(sell_prices)-1)):
                return 0.03  # Reduced penalty
        
        return 0.0

# ============================================================================
# PRICING ENGINE
# ============================================================================

class PricingEngine:
    """Professional pricing with competitive market making"""
    
    def __init__(self):
        self.analytics = {}
        
    def get_analytics(self, instrument: str) -> MarketAnalytics:
        if instrument not in self.analytics:
            self.analytics[instrument] = MarketAnalytics()
        return self.analytics[instrument]
    
    def calculate_spread(self, instrument: str, market_data: MarketData, position: int) -> float:
        """Calculate optimal spread based on market conditions"""
        analytics = self.get_analytics(instrument)
        
        market_spread = market_data.spread
        # More aggressive - use 50% of market spread as base
        base_spread = max(BASE_SPREAD, market_spread * 0.5)
        
        # Volatility adjustment - less sensitive
        vol = analytics.volatility
        vol_multiplier = 1.0 + (vol * 30)  # Reduced from 50
        
        # Position adjustment
        abs_pos = abs(position)
        if abs_pos >= CRITICAL_POSITION:
            pos_multiplier = 1.8 + (abs_pos / POSITION_LIMIT)
        elif abs_pos >= SOFT_POSITION_LIMIT:
            pos_multiplier = 1.3 + ((abs_pos - SOFT_POSITION_LIMIT) / (CRITICAL_POSITION - SOFT_POSITION_LIMIT) * 0.5)
        elif abs_pos >= SAFE_POSITION_LIMIT:
            pos_multiplier = 1.0 + ((abs_pos - SAFE_POSITION_LIMIT) / (SOFT_POSITION_LIMIT - SAFE_POSITION_LIMIT) * 0.3)
        else:
            pos_multiplier = 1.0
        
        # Adverse selection
        adverse_risk = analytics.estimate_adverse_selection_risk(position)
        
        spread = base_spread * vol_multiplier * pos_multiplier + adverse_risk
        spread = max(MIN_SPREAD, min(MAX_SPREAD, spread))
        
        return spread
    
    def calculate_inventory_skew(self, position: int, position_limit: int) -> float:
        """Calculate price skew to manage inventory"""
        if position == 0:
            return 0.0
        
        norm_pos = position / position_limit
        # More aggressive skewing
        skew = -norm_pos * abs(norm_pos) * INVENTORY_DECAY
        skew = max(-MAX_INVENTORY_SKEW, min(MAX_INVENTORY_SKEW, skew))
        
        return skew
    
    def calculate_quote_size(self, position: int, side: str, market_data: MarketData, 
                           outstanding_orders: dict) -> int:
        """Calculate optimal quote size based on position and market depth"""
        abs_pos = abs(position)
        
        # Base size on market depth
        market_size = market_data.bid_size if side == 'bid' else market_data.ask_size
        base_size = min(BASE_QUOTE_SIZE, max(MIN_QUOTE_SIZE, market_size // 2))
        
        # CRITICAL: Only quote on reducing side with max size
        if abs_pos >= CRITICAL_POSITION:
            if (position > 0 and side == 'ask') or (position < 0 and side == 'bid'):
                return MAX_QUOTE_SIZE  # Reduce position aggressively
            else:
                return 0  # Don't add to position
        
        # SOFT LIMIT: Asymmetric sizing
        if abs_pos >= SOFT_POSITION_LIMIT:
            if (position > 0 and side == 'bid') or (position < 0 and side == 'ask'):
                return MIN_QUOTE_SIZE  # Minimal adding
            else:
                return base_size * 2  # Aggressive reducing
        
        # SAFE LIMIT: Start tapering
        if abs_pos >= SAFE_POSITION_LIMIT:
            pos_factor = 1 - ((abs_pos - SAFE_POSITION_LIMIT) / (SOFT_POSITION_LIMIT - SAFE_POSITION_LIMIT) * 0.5)
            if (position > 0 and side == 'bid') or (position < 0 and side == 'ask'):
                return int(base_size * pos_factor)
            else:
                return int(base_size * 1.5)
        
        # Normal quoting
        return base_size
    
    def adjust_for_market(self, bid_price: float, ask_price: float, 
                         market_data: MarketData, position: int) -> Tuple[float, float]:
        """Join or improve best bid/offer strategically"""
        
        abs_pos = abs(position)
        
        # If trying to reduce position, be aggressive (improve market)
        if abs_pos >= SAFE_POSITION_LIMIT:
            if position > 0:  # Need to sell
                ask_price = min(ask_price, market_data.ask - TICK_SIZE)
            elif position < 0:  # Need to buy
                bid_price = max(bid_price, market_data.bid + TICK_SIZE)
        
        # Otherwise join the best price to maximize fill probability
        # Only improve if our theoretical price is better
        if bid_price >= market_data.bid:
            bid_price = market_data.bid
        if ask_price <= market_data.ask:
            ask_price = market_data.ask
        
        return bid_price, ask_price
    
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
        
        # Calculate theoretical prices
        theo_mid = market_data.mid + skew
        bid_price = theo_mid - half_spread
        ask_price = theo_mid + half_spread
        
        # Adjust for market competitively
        bid_price, ask_price = self.adjust_for_market(bid_price, ask_price, market_data, position)
        
        # Round to tick
        bid_price = round(bid_price / TICK_SIZE) * TICK_SIZE
        ask_price = round(ask_price / TICK_SIZE) * TICK_SIZE
        
        # Verify minimum edge
        if (market_data.mid - bid_price) < MIN_EDGE or (ask_price - market_data.mid) < MIN_EDGE:
            # If position is large, accept smaller edge
            if abs(position) < SAFE_POSITION_LIMIT:
                return None
        
        # Calculate desired sizes
        bid_size = self.calculate_quote_size(position, 'bid', market_data, outstanding_orders)
        ask_size = self.calculate_quote_size(position, 'ask', market_data, outstanding_orders)
        
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
    """Safe order management with fast refresh and staleness detection"""
    
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
    
    def is_quote_stale(self, instrument: str, market_data: MarketData) -> bool:
        """Check if our quote is stale relative to market"""
        if instrument not in self.active_quotes:
            return False
        
        quote = self.active_quotes[instrument]
        age = time.time() - quote.timestamp
        
        # If quote is old, it's stale
        if age > STALENESS_THRESHOLD:
            return True
        
        # If market moved significantly against us, it's stale
        if quote.bid_price > market_data.bid + TICK_SIZE * 3:
            logger.debug(f"{instrument}: Stale bid (ours={quote.bid_price}, mkt={market_data.bid})")
            return True
        if quote.ask_price < market_data.ask - TICK_SIZE * 3:
            logger.debug(f"{instrument}: Stale ask (ours={quote.ask_price}, mkt={market_data.ask})")
            return True
        
        return False
    
    def should_refresh_quotes(self, instrument: str, new_quote: Quote) -> bool:
        """Determine if quotes should be refreshed"""
        current_time = time.time()
        
        # Check minimum refresh interval
        if instrument in self.last_refresh:
            if current_time - self.last_refresh[instrument] < MIN_REFRESH_INTERVAL:
                return False
        
        # Always refresh if no previous quote
        if instrument not in self.active_quotes:
            return True
        
        # Refresh on regular interval
        if instrument not in self.last_refresh:
            return True
        
        if current_time - self.last_refresh[instrument] >= ORDER_REFRESH_INTERVAL:
            return True
        
        # Refresh if prices changed significantly
        old_quote = self.active_quotes[instrument]
        
        if abs(new_quote.bid_price - old_quote.bid_price) > TICK_SIZE * 2:
            return True
        if abs(new_quote.ask_price - old_quote.ask_price) > TICK_SIZE * 2:
            return True
        
        # Refresh if sizes changed significantly
        if abs(new_quote.bid_size - old_quote.bid_size) > 10:
            return True
        if abs(new_quote.ask_size - old_quote.ask_size) > 10:
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
            time.sleep(0.01)  # Shorter pause
            
            # Refresh outstanding orders after cancellation
            outstanding_orders = self.exchange.get_outstanding_orders(instrument)
            
            # Place bid order with safety check
            if quote.bid_size > 0:
                can_trade, reason = PositionLimitChecker.can_trade(
                    instrument, 'bid', quote.bid_size, position, outstanding_orders
                )
                
                if can_trade:
                    try:
                        result = self.exchange.insert_order(
                            instrument_id=instrument,
                            price=quote.bid_price,
                            volume=quote.bid_size,
                            side='bid',
                            order_type='limit'
                        )
                        quote.bid_order_id = result
                    except Exception as e:
                        if 'position limit' in str(e).lower():
                            logger.error(f"🚫 {instrument} BID: {e}")
                        else:
                            logger.debug(f"Bid order error {instrument}: {e}")
                else:
                    logger.debug(f"{instrument} BID blocked: {reason}")
            
            # Place ask order with safety check
            if quote.ask_size > 0:
                can_trade, reason = PositionLimitChecker.can_trade(
                    instrument, 'ask', quote.ask_size, position, outstanding_orders
                )
                
                if can_trade:
                    try:
                        result = self.exchange.insert_order(
                            instrument_id=instrument,
                            price=quote.ask_price,
                            volume=quote.ask_size,
                            side='ask',
                            order_type='limit'
                        )
                        quote.ask_order_id = result
                    except Exception as e:
                        if 'position limit' in str(e).lower():
                            logger.error(f"🚫 {instrument} ASK: {e}")
                        else:
                            logger.debug(f"Ask order error {instrument}: {e}")
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
    """Smart delta hedging with multi-stage execution"""
    
    def __init__(self, exchange: Exchange):
        self.exchange = exchange
        self.last_hedge: Dict[str, float] = {}
        self.hedge_orders: Dict[str, list] = defaultdict(list)
    
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
                if time.time() - self.last_hedge[instrument] < 2.0:  # Shorter cooldown
                    return False, "COOLDOWN"
            return True, "NORMAL"
        
        return False, "NONE"
    
    def execute_smart_hedge(self, hedge_instrument: str, net_position: int, urgency: str) -> bool:
        """Multi-stage hedge execution: aggressive IOC + passive limit"""
        try:
            # Get current hedge position
            positions = self.exchange.get_positions()
            hedge_pos = positions.get(hedge_instrument, 0)
            outstanding = self.exchange.get_outstanding_orders(hedge_instrument)
            
            book = self.exchange.get_last_price_book(hedge_instrument)
            if not book or not book.bids or not book.asks:
                return False
            
            # Determine total hedge size
            if urgency == "URGENT":
                total_hedge_size = min(abs(net_position), HEDGE_SIZE * 2)
            else:
                total_hedge_size = min(HEDGE_SIZE, abs(net_position))
            
            if total_hedge_size < MIN_QUOTE_SIZE:
                return False
            
            # Determine side
            side = 'bid' if net_position < 0 else 'ask'
            
            # Check position limits
            can_trade, reason = PositionLimitChecker.can_trade(
                hedge_instrument, side, total_hedge_size, hedge_pos, outstanding
            )
            
            if not can_trade:
                logger.warning(f"⚠️  Hedge blocked for {hedge_instrument}: {reason}")
                return False
            
            # Stage 1: Aggressive IOC (70% of size)
            aggressive_size = int(total_hedge_size * HEDGE_AGGRESSIVE_RATIO)
            if aggressive_size >= MIN_QUOTE_SIZE:
                if side == 'bid':
                    aggressive_price = book.asks[0].price + TICK_SIZE
                else:
                    aggressive_price = book.bids[0].price - TICK_SIZE
                
                try:
                    self.exchange.insert_order(
                        instrument_id=hedge_instrument,
                        price=aggressive_price,
                        volume=aggressive_size,
                        side=side,
                        order_type='ioc'
                    )
                    logger.info(f"🛡️  HEDGE-{urgency}-IOC: {side.upper()} {hedge_instrument} {aggressive_size} @ {aggressive_price:.2f}")
                except Exception as e:
                    logger.error(f"Aggressive hedge error: {e}")
            
            # Stage 2: Passive limit order (30% of size)
            passive_size = total_hedge_size - aggressive_size
            if passive_size >= MIN_QUOTE_SIZE:
                if side == 'bid':
                    passive_price = book.bids[0].price  # Join best bid
                else:
                    passive_price = book.asks[0].price  # Join best ask
                
                try:
                    order_id = self.exchange.insert_order(
                        instrument_id=hedge_instrument,
                        price=passive_price,
                        volume=passive_size,
                        side=side,
                        order_type='limit'
                    )
                    self.hedge_orders[hedge_instrument].append(order_id)
                    logger.info(f"🛡️  HEDGE-{urgency}-LMT: {side.upper()} {hedge_instrument} {passive_size} @ {passive_price:.2f}")
                except Exception as e:
                    logger.error(f"Passive hedge error: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Hedge error: {e}")
            return False
    
    def clean_stale_hedge_orders(self, hedge_instrument: str, max_age: float = 5.0):
        """Cancel stale hedge orders"""
        try:
            outstanding = self.exchange.get_outstanding_orders(hedge_instrument)
            current_time = time.time()
            
            for order_id, order in outstanding.items():
                # Cancel if order is old
                if order_id in self.hedge_orders[hedge_instrument]:
                    try:
                        self.exchange.delete_order(hedge_instrument, order_id)
                        self.hedge_orders[hedge_instrument].remove(order_id)
                    except:
                        pass
        except:
            pass

# ============================================================================
# MAIN MARKET MAKER
# ============================================================================

class ProfessionalMarketMaker:
    """Professional market making system optimized for competitive environment"""
    
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
        logger.info("🏦 PROFESSIONAL MARKET MAKER - COMPETITIVE VERSION v2.0")
        logger.info("=" * 80)
        logger.info(f"📊 Config: Spread={MIN_SPREAD:.3f}-{MAX_SPREAD:.2f}, Size={MIN_QUOTE_SIZE}-{MAX_QUOTE_SIZE}")
        logger.info(f"📊 Position Limits: Safe={SAFE_POSITION_LIMIT}, Soft={SOFT_POSITION_LIMIT}, Critical={CRITICAL_POSITION}")
        logger.info(f"📊 Performance: Loop={LOOP_INTERVAL*1000:.0f}ms, Refresh={ORDER_REFRESH_INTERVAL*1000:.0f}ms")
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
                out_str = f"Out:[B{out_buy:2d} S{out_sell:2d}]"
            except:
                out_str = "Out:[N/A]"
            
            pos_pct = abs(pos) / POSITION_LIMIT * 100
            if abs(pos) >= CRITICAL_POSITION:
                pos_status = "🔴 CRIT"
            elif abs(pos) >= SOFT_POSITION_LIMIT:
                pos_status = "🟡 HIGH"
            elif abs(pos) >= SAFE_POSITION_LIMIT:
                pos_status = "🟠 WARN"
            else:
                pos_status = "🟢 OK  "
            
            quote = self.order_manager.active_quotes.get(instrument)
            if quote:
                spread = quote.ask_price - quote.bid_price
                quote_str = f"[{quote.bid_price:.2f}x{quote.bid_size:2d} / {quote.ask_size:2d}x{quote.ask_price:.2f}] Δ={spread:.3f}"
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
            
            # Check for stale quotes
            try:
                market_data = self.order_manager.get_market_data(instrument)
                if market_data and self.order_manager.is_quote_stale(instrument, market_data):
                    logger.warning(f"⏰ {instrument}: Stale quote detected, cancelling")
                    self.order_manager.cancel_all_orders(instrument)
            except:
                pass
    
    def run(self):
        logger.info("🚀 Starting competitive market making...")
        
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
                        if self.hedge_manager.execute_smart_hedge(hedge_instrument, net_pos, urgency):
                            self.hedge_manager.last_hedge[instrument] = time.time()
                    
                    # Clean stale hedge orders
                    self.hedge_manager.clean_stale_hedge_orders(hedge_instrument)
                
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
                    time.sleep(0.1)
        
        # Clean shutdown
        logger.info("🧹 Cancelling all orders...")
        for instrument in INSTRUMENTS:
            self.order_manager.cancel_all_orders(instrument)
            if instrument in HEDGE_PAIRS:
                self.order_manager.cancel_all_orders(HEDGE_PAIRS[instrument])
        
        # Final P&L report
        pnls = self.calculate_pnl()
        total_pnl = sum(pnls.values())
        
        logger.info("=" * 80)
        logger.info("🏁 FINAL P&L REPORT")
        logger.info("-" * 80)
        for instrument in INSTRUMENTS:
            pnl = pnls.get(instrument, 0.0)
            logger.info(f"  {instrument:4s}: ${pnl:+,.2f}")
        logger.info("-" * 80)
        logger.info(f"  TOTAL: ${total_pnl:+,.2f}")
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