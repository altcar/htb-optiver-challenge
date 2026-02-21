#!/usr/bin/env python3
"""
AMM v8 — Optimized Spread Capture (Fixed Position Management)

Key improvements:
- Fixed position limit breaches from spoofing
- Smarter spoof sizing that respects limits
- Better spread capture tracking
- More aggressive spread widening
- Improved profit maximization
"""
from __future__ import annotations
import time
import logging
import argparse
import os
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Dict, Optional, Any, Tuple, List, Set
import random

import numpy as np

from optibook.synchronous_client import Exchange

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("amm_optimized")
logging.getLogger("client").setLevel("ERROR")

KILL_SWITCH_FILE = "/tmp/amm_kill_switch"

def round_down_to_tick(price: float, tick: float) -> float:
    return float(np.floor(price / tick) * tick)

def round_up_to_tick(price: float, tick: float) -> float:
    return float(np.ceil(price / tick) * tick)

@dataclass
class OrderState:
    order_id: Any
    instrument: str
    side: str
    price: float
    volume: int
    placed_ts: float
    is_spoof: bool = False
    cancel_after: float = 0.0

@dataclass
class SpreadStats:
    total_captured: float = 0.0
    num_trades: int = 0
    best_spread: float = 0.0
    last_update: float = 0.0
    
    @property
    def avg_spread(self) -> float:
        return self.total_captured / max(self.num_trades, 1)

class RateLimiter:
    def __init__(self, max_calls: int = 12):
        self.max_calls = max_calls
        self.calls = deque()
        
    def acquire(self) -> bool:
        now = time.time()
        while self.calls and now - self.calls[0] > 1.0:
            self.calls.popleft()
        if len(self.calls) >= self.max_calls:
            time.sleep(0.15)
            return False
        self.calls.append(now)
        return True

class SmartSpoofer:
    """Improved spoofing that respects position limits"""
    def __init__(self, tick: float):
        self.tick = tick
        self.last_spoof = 0.0
        self.cooldown = 4.0
        self.active_spoofs: Set[Any] = set()
        
    def should_spoof(self, net_pos: int, pos_limit: int, current_exposure: int) -> Tuple[bool, str]:
        """Decide if we should spoof, considering current exposure"""
        now = time.time()
        
        if now - self.last_spoof < self.cooldown:
            return False, ""
            
        # Don't spoof if we're already exposed
        if abs(current_exposure) > 30:
            return False, ""
            
        # Don't spoof near position limits
        if abs(net_pos) > pos_limit * 0.6:
            return False, ""
            
        # Spoof based on position
        if net_pos > 15:
            return True, "bid"  # Push price up
        elif net_pos < -15:
            return True, "ask"  # Push price down
        else:
            # Random spoofing for volatility
            if random.random() > 0.8:
                return True, random.choice(["bid", "ask"])
                
        return False, ""
        
    def get_safe_spoof_size(self, net_pos: int, pos_limit: int, exchange_limit: int, 
                            current_inst_pos: int) -> int:
        """Calculate safe spoof size that won't breach limits"""
        # Maximum theoretical size before hitting ANY limit
        to_exchange_limit = exchange_limit - abs(current_inst_pos) - 20
        to_net_limit = pos_limit - abs(net_pos) - 15
        
        # Take the most conservative
        max_safe = min(to_exchange_limit, to_net_limit, 25)
        
        # Random size within safe range
        if max_safe > 0:
            return random.randint(max(3, max_safe // 3), max(5, max_safe // 2))
        return 0
        
    def generate_spoofs(self, side: str, base_price: float, safe_size: int) -> List[Tuple[float, int]]:
        """Generate spoof orders with safe sizing"""
        if safe_size <= 0:
            return []
            
        spoofs = []
        num_levels = min(2, safe_size // 5)  # Fewer levels
        
        for i in range(max(1, num_levels)):
            if side == "bid":
                price = round_up_to_tick(base_price + (i + 1) * self.tick, self.tick)
            else:
                price = round_down_to_tick(base_price - (i + 1) * self.tick, self.tick)
            
            # Distribute size across levels
            size = max(3, safe_size // max(1, num_levels))
            spoofs.append((price, size))
            
        self.last_spoof = time.time()
        return spoofs
        
    def record_spoof(self, order_id: Any):
        self.active_spoofs.add(order_id)
        
    def clear(self):
        self.active_spoofs.clear()

class SpreadOptimizer:
    """Optimize spread to maximize profit"""
    def __init__(self):
        self.market_spreads = deque(maxlen=30)
        self.our_fills = deque(maxlen=50)
        
    def record_market_spread(self, spread: float):
        self.market_spreads.append(spread)
        
    def record_fill(self, our_spread: float, captured: float):
        self.our_fills.append({"spread": our_spread, "captured": captured})
        
    def get_optimal_multiplier(self, base_mult: float, net_pos: int, pos_limit: int) -> float:
        """Calculate optimal spread multiplier"""
        mult = base_mult
        
        # Analyze market conditions
        if len(self.market_spreads) >= 10:
            recent = list(self.market_spreads)[-10:]
            avg_market = np.mean(recent)
            std_market = np.std(recent)
            
            # If market is wide, we can be wider
            if avg_market > 0.3:
                mult *= 1.4
            
            # If volatile, capture more
            if std_market > 0.1:
                mult *= 1.2
                
        # Check our fill history
        if len(self.our_fills) >= 10:
            recent = list(self.our_fills)[-10:]
            avg_captured = np.mean([f["captured"] for f in recent])
            
            # If we're capturing good spreads, try wider
            if avg_captured > 0.15:
                mult *= 1.3
                
        # Position-based adjustment
        pos_ratio = abs(net_pos) / max(pos_limit, 1)
        if pos_ratio > 0.7:
            mult *= 0.6  # Tighten to trade out
        elif pos_ratio < 0.3:
            mult *= 1.2  # Can be wider
            
        # Cap it
        return min(mult, 6.0)

class OptimizedMarketMaker:
    def __init__(self,
                 exchange: Exchange,
                 main: str = "NVDA",
                 dual: Optional[str] = None,
                 net_pos_limit: int = 80):
        self.exchange = exchange
        self.main = main
        self.dual = dual or f"{main}_DUAL"
        
        # Components
        self.spoofer = SmartSpoofer(tick=0.10)
        self.optimizer = SpreadOptimizer()
        self.rate_limiter = RateLimiter(max_calls=12)
        
        # Parameters
        self.TICK = 0.10
        self.NET_POS_LIMIT = net_pos_limit
        self.EXCHANGE_LIMIT = 100
        self.BASE_SIZE = 5
        self.BASE_SPREAD_MULT = 3.0  # Start at 3x
        
        # State
        self.outstanding: Dict[Any, OrderState] = {}
        self._cached_books = (None, None)
        self._cached_positions = {}
        self.last_book_poll = 0.0
        self.last_pos_poll = 0.0
        self.BOOK_TTL = 0.4
        self.POS_TTL = 0.8
        
        # Stats
        self.spread_stats = SpreadStats()
        self.last_order_time = 0.0
        self.min_order_interval = 0.05
        
        logger.info(f"OptimizedMarketMaker: {self.main}/{self.dual} limit={self.NET_POS_LIMIT}")

    def clear_startup_positions(self):
        """Clear positions at startup"""
        logger.info("="*60)
        logger.info("CLEARING STARTUP POSITIONS")
        logger.info("="*60)
        
        try:
            # Cancel all orders
            for inst in [self.main, self.dual]:
                try:
                    orders = self.exchange.get_outstanding_orders(inst)
                    for oid in orders:
                        try:
                            self.exchange.delete_order(instrument_id=inst, order_id=oid)
                            time.sleep(0.03)
                        except:
                            pass
                except:
                    pass
                    
            time.sleep(0.5)
            
            # Clear positions
            positions = self.exchange.get_positions()
            
            for inst in [self.main, self.dual]:
                pos = positions.get(inst, 0)
                if pos != 0:
                    logger.info(f"Clearing {inst}: {pos}")
                    ob = self.exchange.get_last_price_book(inst)
                    if ob and ob.bids and ob.asks:
                        if pos > 0:
                            price = ob.bids[0].price - self.TICK
                            self.exchange.insert_order(inst, price=price, volume=abs(pos), 
                                                      side="ask", order_type="ioc")
                        else:
                            price = ob.asks[0].price + self.TICK
                            self.exchange.insert_order(inst, price=price, volume=abs(pos),
                                                      side="bid", order_type="ioc")
                        time.sleep(0.3)
                        
            time.sleep(0.5)
            final = self.exchange.get_positions()
            logger.info(f"Final: MAIN={final.get(self.main, 0)} DUAL={final.get(self.dual, 0)}")
            logger.info("="*60)
            
        except Exception as e:
            logger.error(f"Clear failed: {e}")

    def ensure_connection(self) -> bool:
        try:
            self.exchange.get_positions()
            return True
        except:
            try:
                new_ex = Exchange()
                new_ex.connect()
                self.exchange = new_ex
                self.outstanding.clear()
                time.sleep(0.2)
                self.exchange.get_positions()
                logger.info("Reconnected")
                return True
            except:
                return False

    def get_books(self) -> Tuple[Optional[Any], Optional[Any]]:
        now = time.time()
        if now - self.last_book_poll > self.BOOK_TTL:
            try:
                ob_main = self.exchange.get_last_price_book(self.main)
                ob_dual = self.exchange.get_last_price_book(self.dual)
                self._cached_books = (ob_main, ob_dual)
                self.last_book_poll = now
            except:
                if self.ensure_connection():
                    try:
                        ob_main = self.exchange.get_last_price_book(self.main)
                        ob_dual = self.exchange.get_last_price_book(self.dual)
                        self._cached_books = (ob_main, ob_dual)
                        self.last_book_poll = now
                    except:
                        pass
        return self._cached_books

    def get_positions(self) -> Dict[str, int]:
        now = time.time()
        if now - self.last_pos_poll > self.POS_TTL:
            try:
                self._cached_positions = self.exchange.get_positions()
            except:
                self._cached_positions = {}
            self.last_pos_poll = now
        return self._cached_positions

    def positions_tuple(self) -> Tuple[int, int, int]:
        p = self.get_positions()
        pm = p.get(self.main, 0)
        pd = p.get(self.dual, 0)
        return pm, pd, pm + pd

    def throttle(self):
        if not self.rate_limiter.acquire():
            time.sleep(0.2)
        now = time.time()
        elapsed = now - self.last_order_time
        if elapsed < self.min_order_interval:
            time.sleep(self.min_order_interval - elapsed)
        self.last_order_time = time.time()

    def place_order(self, inst: str, price: float, size: int, side: str, 
                   is_spoof: bool = False, cancel_after: float = 0.0) -> Optional[OrderState]:
        self.throttle()
        
        try:
            res = self.exchange.insert_order(inst, price=price, volume=size, 
                                            side=side, order_type="limit")
            oid = getattr(res, "order_id", None)
            if oid:
                os = OrderState(order_id=oid, instrument=inst, side=side, price=price,
                              volume=size, placed_ts=time.time(), is_spoof=is_spoof,
                              cancel_after=cancel_after)
                self.outstanding[oid] = os
                if is_spoof:
                    self.spoofer.record_spoof(oid)
                return os
        except Exception as e:
            if "not connected" in str(e).lower():
                self.ensure_connection()
        return None

    def delete_order(self, inst: str, oid: Any) -> bool:
        try:
            self.exchange.delete_order(instrument_id=inst, order_id=oid)
            return True
        except:
            return False

    def compute_quotes(self, ob_main, ob_dual) -> Optional[dict]:
        """Compute quotes with optimized spread"""
        if not (ob_main and ob_main.bids and ob_main.asks):
            return None
        if not (ob_dual and ob_dual.bids and ob_dual.asks):
            return None

        pm, pd, net = self.positions_tuple()

        # Fair value
        mid_main = (ob_main.bids[0].price + ob_main.asks[0].price) / 2.0
        mid_dual = (ob_dual.bids[0].price + ob_dual.asks[0].price) / 2.0
        fair = (mid_main + mid_dual) / 2.0
        
        # Market spread
        mkt_spread = ob_main.asks[0].price - ob_main.bids[0].price
        self.optimizer.record_market_spread(mkt_spread)
        
        # Get optimal spread multiplier
        spread_mult = self.optimizer.get_optimal_multiplier(
            self.BASE_SPREAD_MULT, net, self.NET_POS_LIMIT)
        
        # Calculate our spread
        our_spread = self.TICK * spread_mult
        
        # Position skew
        if abs(net) > self.NET_POS_LIMIT * 0.4:
            skew = -np.sign(net) * (abs(net) / self.NET_POS_LIMIT) * self.TICK * 2.5
        else:
            skew = 0.0
            
        # Our prices
        half = our_spread / 2.0
        bid = round_down_to_tick(fair + skew - half, self.TICK)
        ask = round_up_to_tick(fair + skew + half, self.TICK)
        
        # Don't cross market
        if bid >= ob_main.asks[0].price - self.TICK:
            bid = round_down_to_tick(ob_main.asks[0].price - self.TICK, self.TICK)
        if ask <= ob_main.bids[0].price + self.TICK:
            ask = round_up_to_tick(ob_main.bids[0].price + self.TICK, self.TICK)
            
        # Sizing
        if net > self.NET_POS_LIMIT * 0.6:
            bid_sz = max(1, self.BASE_SIZE // 3)
            ask_sz = self.BASE_SIZE * 2
        elif net < -self.NET_POS_LIMIT * 0.6:
            bid_sz = self.BASE_SIZE * 2
            ask_sz = max(1, self.BASE_SIZE // 3)
        else:
            bid_sz = ask_sz = self.BASE_SIZE
            
        # Position checks
        if pm + bid_sz > self.EXCHANGE_LIMIT - 20:
            bid_sz = max(0, self.EXCHANGE_LIMIT - 20 - pm)
        if pm - ask_sz < -self.EXCHANGE_LIMIT + 20:
            ask_sz = max(0, pm + self.EXCHANGE_LIMIT - 20)
        if net + bid_sz > self.NET_POS_LIMIT - 10:
            bid_sz = max(0, self.NET_POS_LIMIT - 10 - net)
        if net - ask_sz < -self.NET_POS_LIMIT + 10:
            ask_sz = max(0, net + self.NET_POS_LIMIT - 10)
            
        if bid_sz <= 0 and ask_sz <= 0:
            return None
            
        return {
            "bid": bid, "ask": ask,
            "bid_sz": bid_sz, "ask_sz": ask_sz,
            "spread": ask - bid,
            "spread_mult": spread_mult,
            "mkt_spread": mkt_spread,
            "net": net,
        }

    def execute_spoofing(self, ob_main, net: int, pm: int):
        """Execute smart spoofing"""
        # Calculate current exposure from outstanding orders
        bid_exposure = sum(o.volume for o in self.outstanding.values() 
                          if o.side == "bid" and o.instrument == self.main)
        ask_exposure = sum(o.volume for o in self.outstanding.values()
                          if o.side == "ask" and o.instrument == self.main)
        
        should, direction = self.spoofer.should_spoof(net, self.NET_POS_LIMIT, 
                                                      bid_exposure - ask_exposure)
        if not should:
            return
            
        # Calculate safe size
        safe_size = self.spoofer.get_safe_spoof_size(net, self.NET_POS_LIMIT,
                                                     self.EXCHANGE_LIMIT, pm)
        if safe_size <= 0:
            logger.debug(f"No safe spoof size available (net={net}, pm={pm})")
            return
            
        # Generate spoofs
        if direction == "bid" and ob_main.bids:
            spoofs = self.spoofer.generate_spoofs("bid", ob_main.bids[0].price, safe_size)
            logger.info(f"🎭 SPOOF: {len(spoofs)} bids, total_size={sum(s for _,s in spoofs)}, safe={safe_size}")
        elif direction == "ask" and ob_main.asks:
            spoofs = self.spoofer.generate_spoofs("ask", ob_main.asks[0].price, safe_size)
            logger.info(f"🎭 SPOOF: {len(spoofs)} asks, total_size={sum(s for _,s in spoofs)}, safe={safe_size}")
        else:
            return
            
        # Place spoofs
        for price, size in spoofs:
            cancel_time = time.time() + 1.5  # Cancel after 1.5s
            self.place_order(self.main, price, size, direction, 
                           is_spoof=True, cancel_after=cancel_time)
            time.sleep(0.05)

    def cancel_stale_spoofs(self):
        """Cancel expired spoof orders"""
        now = time.time()
        to_cancel = []
        
        for oid, order in list(self.outstanding.items()):
            if order.is_spoof and order.cancel_after > 0 and now >= order.cancel_after:
                to_cancel.append((order.instrument, oid))
            elif now - order.placed_ts > 25.0:  # Cancel very old orders
                to_cancel.append((order.instrument, oid))
                
        for inst, oid in to_cancel:
            if self.delete_order(inst, oid):
                self.outstanding.pop(oid, None)

    def cancel_all(self):
        for oid, o in list(self.outstanding.items()):
            self.delete_order(o.instrument, oid)
        self.outstanding.clear()
        self.spoofer.clear()

    def detect_fills(self, prev_net: int, quote: Optional[dict]):
        """Detect fills and track spread capture"""
        _, _, net = self.positions_tuple()
        delta = net - prev_net
        
        if delta == 0:
            return
            
        direction = "BUY" if delta > 0 else "SELL"
        
        ob_main, _ = self.get_books()
        if ob_main and ob_main.bids and ob_main.asks:
            mid = (ob_main.bids[0].price + ob_main.asks[0].price) / 2.0
            
            if quote:
                if direction == "BUY":
                    fill_price = quote["bid"]
                    spread_captured = mid - fill_price
                else:
                    fill_price = quote["ask"]
                    spread_captured = fill_price - mid
                    
                # Update stats
                self.spread_stats.total_captured += spread_captured * abs(delta)
                self.spread_stats.num_trades += abs(delta)
                self.spread_stats.last_update = time.time()
                
                if spread_captured > self.spread_stats.best_spread:
                    self.spread_stats.best_spread = spread_captured
                    
                # Record for optimizer
                our_spread = quote["spread"]
                self.optimizer.record_fill(our_spread, spread_captured)
                
                logger.info(f"💰 {direction} {abs(delta)} @ ~{fill_price:.2f} | "
                          f"Captured: ${spread_captured:.3f} | "
                          f"Total: ${self.spread_stats.total_captured:.2f} | "
                          f"Avg: ${self.spread_stats.avg_spread:.3f} | "
                          f"Net: {net}")

    def run(self, duration: float = 600.0):
        """Main loop"""
        self.clear_startup_positions()
        
        logger.info("="*60)
        logger.info("STARTING OPTIMIZED MARKET MAKER")
        logger.info("="*60)
        
        start = time.time()
        iteration = 0
        
        try:
            while time.time() - start < duration:
                if os.path.exists(KILL_SWITCH_FILE):
                    break
                    
                iteration += 1
                
                if not self.ensure_connection():
                    time.sleep(1.0)
                    continue
                    
                ob_main, ob_dual = self.get_books()
                if not ob_main or not ob_dual:
                    time.sleep(0.3)
                    continue
                    
                pm, pd, net_before = self.positions_tuple()
                
                # Compute quotes
                quote = self.compute_quotes(ob_main, ob_dual)
                
                if quote:
                    # Log periodically
                    if iteration % 5 == 0:
                        logger.info(f"📊 Spread: {quote['spread_mult']:.1f}x "
                                  f"(${quote['spread']:.2f}) | "
                                  f"Market: ${quote['mkt_spread']:.2f} | "
                                  f"Pos: {net_before}")
                    
                    # Cancel expired spoofs
                    self.cancel_stale_spoofs()
                    
                    # Place main quotes
                    if quote["bid_sz"] > 0:
                        self.place_order(self.main, quote["bid"], quote["bid_sz"], "bid")
                    if quote["ask_sz"] > 0:
                        self.place_order(self.main, quote["ask"], quote["ask_sz"], "ask")
                        
                    # Place dual quotes (slightly offset)
                    dual_bid = round_down_to_tick(quote["bid"] - 0.5 * self.TICK, self.TICK)
                    dual_ask = round_up_to_tick(quote["ask"] + 0.5 * self.TICK, self.TICK)
                    
                    if quote["bid_sz"] > 0:
                        self.place_order(self.dual, dual_bid, quote["bid_sz"], "bid")
                    if quote["ask_sz"] > 0:
                        self.place_order(self.dual, dual_ask, quote["ask_sz"], "ask")
                    
                    # Execute spoofing every 4 iterations
                    if iteration % 4 == 0:
                        self.execute_spoofing(ob_main, net_before, pm)
                
                time.sleep(0.2)
                
                # Detect fills
                _, _, net_after = self.positions_tuple()
                self.detect_fills(net_before, quote)
                
                time.sleep(0.4)
                
        except KeyboardInterrupt:
            logger.info("Interrupted")
        finally:
            logger.info("="*60)
            logger.info("SHUTTING DOWN")
            logger.info("="*60)
            self.cancel_all()
            
            # Final stats
            logger.info("="*60)
            logger.info("SPREAD CAPTURE RESULTS")
            logger.info("="*60)
            logger.info(f"Total Captured: ${self.spread_stats.total_captured:.2f}")
            logger.info(f"Trades: {self.spread_stats.num_trades}")
            logger.info(f"Average Spread: ${self.spread_stats.avg_spread:.3f}")
            logger.info(f"Best Spread: ${self.spread_stats.best_spread:.3f}")
            logger.info("="*60)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--main", default="NVDA")
    parser.add_argument("--dual", default=None)
    parser.add_argument("--net-limit", type=int, default=80)
    parser.add_argument("--duration", type=int, default=600)
    args = parser.parse_args()
    
    ex = Exchange()
    try:
        ex.connect()
    except:
        pass
        
    mm = OptimizedMarketMaker(ex, main=args.main, dual=args.dual, 
                             net_pos_limit=args.net_limit)
    mm.run(duration=args.duration)

if __name__ == "__main__":
    main()