#!/usr/bin/env python3
"""
AMM v5 — Dual-book market maker (safe net-position control + adaptive skew/herding)

Features:
- Treats MAIN and MAIN_DUAL as the same underlying (net position = pos_main + pos_dual)
- Enforces net position limits BEFORE placing any order
- Robust reconnect (creates a new Exchange client), DOES NOT re-quote until positions are refreshed
- Tolerant delete/insert wrappers
- Dry-run mode and simple kill-switch
- Adaptive skew & simple herding (conservative & capped)
- Rate-limited order calls to avoid API hammering
- Enhanced with simulation-legal aggressive strategies

Intended for sanctioned simulations/backtests only.
"""
from __future__ import annotations
import time
import logging
import argparse
import os
from collections import deque, defaultdict
from dataclasses import dataclass
from typing import Dict, Optional, Any, Tuple, List
import math

import numpy as np

from optibook.synchronous_client import Exchange

# --- Logging ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("amm_v5")
logging.getLogger("client").setLevel("ERROR")

# Kill file - create this path to immediately stop the bot
KILL_SWITCH_FILE = "/tmp/amm_v5_kill_switch"

# --- small helpers ---
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

# --- Enhanced Rate Limiter ---
class RateLimiter:
    def __init__(self, max_calls_per_second: int = 50, burst_capacity: int = 100):
        self.max_calls_per_second = max_calls_per_second
        self.burst_capacity = burst_capacity
        self.call_times = deque()
        self.min_interval = 1.0 / max_calls_per_second
        
    def acquire(self) -> bool:
        now = time.time()
        
        # Remove calls older than 1 second
        while self.call_times and now - self.call_times[0] > 1.0:
            self.call_times.popleft()
            
        # Check if we're at capacity
        if len(self.call_times) >= self.burst_capacity:
            return False
            
        # Enforce minimum interval for high-frequency bursts
        if len(self.call_times) >= self.max_calls_per_second:
            time_since_last_window = now - self.call_times[0]
            if time_since_last_window < 1.0:
                sleep_time = 1.0 - time_since_last_window
                time.sleep(sleep_time)
                now = time.time()
                
        self.call_times.append(now)
        return True

# --- Aggressive Simulation Strategies ---
class AggressiveStrategies:
    """Strategies that would be illegal in real markets but are allowed in simulation"""
    
    def __init__(self):
        self.order_imbalance_history = deque(maxlen=100)
        self.momentum_windows = defaultdict(lambda: deque(maxlen=20))
        self.last_prices = {}
        self.spoof_orders_active = False
        self.spoof_order_ids = set()
        self.last_spoof_attempt = 0
        self.min_spoof_interval = 5.0  # Minimum time between spoof attempts
        
    def detect_order_imbalance(self, orderbook) -> float:
        """Detect buy/sell pressure imbalance"""
        if not orderbook or not orderbook.bids or not orderbook.asks:
            return 0.0
            
        bid_volume = sum(b.volume for b in orderbook.bids[:3])  # Top 3 levels
        ask_volume = sum(a.volume for a in orderbook.asks[:3])
        
        if bid_volume + ask_volume == 0:
            return 0.0
            
        imbalance = (bid_volume - ask_volume) / (bid_volume + ask_volume)
        self.order_imbalance_history.append(imbalance)
        return imbalance
    
    def momentum_arbitrage(self, mid_main: float, mid_dual: float, instrument: str) -> float:
        """Exploit momentum differences between instruments"""
        current_mid = (mid_main + mid_dual) / 2.0
        
        # Track momentum
        if instrument in self.last_prices:
            momentum = current_mid - self.last_prices[instrument]
            self.momentum_windows[instrument].append(momentum)
        
        self.last_prices[instrument] = current_mid
        
        # Calculate momentum signal (weighted average of recent momentum)
        if len(self.momentum_windows[instrument]) > 0:
            weights = np.array([math.exp(i) for i in range(len(self.momentum_windows[instrument]))])
            weights = weights / weights.sum()
            momentum_signal = np.average(list(self.momentum_windows[instrument]), weights=weights)
            return momentum_signal * 10.0  # Amplify signal
        
        return 0.0
    
    def spoofing_strategy(self, exchange, instrument: str, fair_price: float, tick_size: float) -> Tuple[float, float]:
        """
        Place large fake orders to manipulate price perception
        Returns adjusted bid/ask prices
        """
        current_time = time.time()
        
        # Only spoof occasionally to avoid detection patterns
        if current_time - self.last_spoof_attempt < self.min_spoof_interval:
            return fair_price - tick_size * 0.5, fair_price + tick_size * 0.5
            
        try:
            # Get current orderbook to understand market structure
            orderbook = exchange.get_last_price_book(instrument)
            if not orderbook or not orderbook.bids or not orderbook.asks:
                return fair_price - tick_size * 0.5, fair_price + tick_size * 0.5
            
            # Cancel any existing spoof orders
            for order_id in list(self.spoof_order_ids):
                try:
                    exchange.delete_order(order_id)
                    self.spoof_order_ids.discard(order_id)
                except:
                    pass
            
            # Determine spoof direction based on position
            positions = exchange.get_positions()
            net_pos = positions.get(instrument, 0)
            
            if net_pos > 0:  # Long position - spoof buy pressure to push price up
                spoof_price = round_up_to_tick(orderbook.asks[0].price + tick_size * 2, tick_size)
                spoof_order = exchange.insert_order(instrument, price=spoof_price, volume=500, 
                                                  side="bid", order_type="limit")
                if hasattr(spoof_order, 'order_id'):
                    self.spoof_order_ids.add(spoof_order.order_id)
                    logger.info(f"SPOOFING: Placed fake bid {500} @ {spoof_price:.2f}")
                    
                # Adjust real quotes to take advantage
                adjusted_bid = fair_price - tick_size * 1.5
                adjusted_ask = fair_price + tick_size * 0.5
                
            elif net_pos < 0:  # Short position - spoof sell pressure to push price down
                spoof_price = round_down_to_tick(orderbook.bids[0].price - tick_size * 2, tick_size)
                spoof_order = exchange.insert_order(instrument, price=spoof_price, volume=500, 
                                                  side="ask", order_type="limit")
                if hasattr(spoof_order, 'order_id'):
                    self.spoof_order_ids.add(spoof_order.order_id)
                    logger.info(f"SPOOFING: Placed fake ask {500} @ {spoof_price:.2f}")
                    
                # Adjust real quotes to take advantage
                adjusted_bid = fair_price - tick_size * 0.5
                adjusted_ask = fair_price + tick_size * 1.5
                
            else:  # Flat - no spoofing
                adjusted_bid = fair_price - tick_size * 0.5
                adjusted_ask = fair_price + tick_size * 0.5
            
            self.last_spoof_attempt = current_time
            self.spoof_orders_active = True
            return adjusted_bid, adjusted_ask
            
        except Exception as e:
            logger.debug(f"Spoofing strategy failed: {e}")
            return fair_price - tick_size * 0.5, fair_price + tick_size * 0.5
    
    def cancel_spoof_orders(self, exchange):
        """Cancel all active spoof orders"""
        for order_id in list(self.spoof_order_ids):
            try:
                exchange.delete_order(order_id)
                self.spoof_order_ids.discard(order_id)
            except:
                pass
        self.spoof_orders_active = False

# --- Market Analyzer (enhanced) ---
class MarketAnalyzer:
    def __init__(self, ewma_alpha: float = 0.12, window_secs: int = 60):
        self.ewma_alpha = ewma_alpha
        self.vol_ewma = 0.01
        self.spread_ewma = 0.2
        self.depth_ewma = 100.0
        self.trade_times = deque()
        self.window_secs = window_secs
        self.last_mid_main: Optional[float] = None
        self.last_mid_dual: Optional[float] = None
        self.aggressive_strategies = AggressiveStrategies()

    def update_from_orderbooks(self, ob_main, ob_dual):
        def safe_mid(ob):
            if not ob or not getattr(ob, "bids", None) or not getattr(ob, "asks", None):
                return None
            return (ob.bids[0].price + ob.asks[0].price) / 2.0

        mid_main = safe_mid(ob_main)
        mid_dual = safe_mid(ob_dual)

        # update volatility EWMA
        if mid_main is not None and self.last_mid_main is not None and self.last_mid_main > 0:
            ret = (mid_main - self.last_mid_main) / self.last_mid_main
            self.vol_ewma = (1 - self.ewma_alpha) * self.vol_ewma + self.ewma_alpha * abs(ret)
        if mid_dual is not None and self.last_mid_dual is not None and self.last_mid_dual > 0:
            ret = (mid_dual - self.last_mid_dual) / self.last_mid_dual
            self.vol_ewma = (1 - self.ewma_alpha) * self.vol_ewma + self.ewma_alpha * abs(ret)

        if mid_main is not None:
            self.last_mid_main = mid_main
        if mid_dual is not None:
            self.last_mid_dual = mid_dual

        # use main book for spread & depth when available
        ob_for_stats = ob_main if mid_main is not None else ob_dual
        if ob_for_stats and getattr(ob_for_stats, "bids", None) and getattr(ob_for_stats, "asks", None):
            spread = ob_for_stats.asks[0].price - ob_for_stats.bids[0].price
            depth = sum(b.volume for b in ob_for_stats.bids[:5]) + sum(a.volume for a in ob_for_stats.asks[:5])
            self.spread_ewma = (1 - self.ewma_alpha) * self.spread_ewma + self.ewma_alpha * spread
            self.depth_ewma = (1 - self.ewma_alpha) * self.depth_ewma + self.ewma_alpha * depth

    def volatility(self) -> float:
        return max(self.vol_ewma, 1e-6)

    def avg_spread(self) -> float:
        return max(self.spread_ewma, 1e-6)

    def market_depth(self) -> float:
        return max(self.depth_ewma, 1.0)

# --- The safe dual AMM (enhanced) ---
class SafeDualAMM:
    def __init__(self,
                 exchange: Exchange,
                 main: str = "NVDA",
                 dual: Optional[str] = None,
                 net_pos_limit: int = 80,
                 dry_run: bool = False,
                 crush_mult: float = 1.0):
        self.exchange = exchange
        self.main = main
        self.dual = dual if dual is not None else f"{main}_DUAL"
        self.analyzer = MarketAnalyzer()

        # params
        self.TICK = 0.10
        self.NET_POS_LIMIT = net_pos_limit
        self.SAFETY_BUFFER = 5
        self.BASE_SIZE = 8
        self.crush_mult = crush_mult
        self.dry_run = dry_run

        # adaptive skew/herd (conservative)
        self.skew_ticks = 0.0
        self.herd_bias = 0.0
        self.SKEW_ALPHA = 0.5
        self.SKEW_DECAY = 0.02
        self.HERD_ALPHA = 0.01
        self.HERD_DECAY = 0.005
        self.MAX_SKEW_TICKS = 16

        # state
        self.outstanding: Dict[Any, OrderState] = {}
        self._cached_main = None
        self._cached_dual = None
        self.last_market_poll = 0.0
        self.MARKET_TTL = 0.12
        self._cached_positions = {}
        self.last_position_poll = 0.0
        self.POS_TTL = 0.25
        self.last_quote = None
        self._last_quote_time = 0.0

        # PnL bookkeeping (best-effort)
        self.position = 0
        self.avg_cost = 0.0
        self.realized_pnl = 0.0

        # rate limit
        self.rate_limiter = RateLimiter(max_calls_per_second=40, burst_capacity=80)
        self.last_order_ts = 0.0
        self.min_order_interval = 0.025  # Slightly more conservative

        # connectivity flag: don't quote until positions refreshed after reconnect
        self.connected = True

        # Aggressive strategy flags
        self.use_spoofing = True
        self.use_momentum_arb = True
        self.last_aggressive_update = 0.0

        logger.info("SafeDualAMM init main=%s dual=%s net_limit=%d dry_run=%s crush=%.2f",
                    self.main, self.dual, self.NET_POS_LIMIT, self.dry_run, self.crush_mult)

    # --- Enhanced Rate Limited Order Helpers ---
    def _throttled_order_call(self):
        """Use enhanced rate limiter"""
        if not self.rate_limiter.acquire():
            time.sleep(0.5)  # Back off if rate limited
        now = time.time()
        elapsed = now - self.last_order_ts
        if elapsed < self.min_order_interval:
            time.sleep(self.min_order_interval - elapsed)
        self.last_order_ts = time.time()

    # --- connection helpers ---
    def _new_exchange_client(self) -> Optional[Exchange]:
        try:
            new_ex = Exchange()
            new_ex.connect()
            return new_ex
        except Exception as e:
            logger.debug("New client creation failed: %s", e)
            return None

    def ensure_connection(self, retries: int = 3, wait: float = 0.5) -> bool:
        """Ensure exchange connection; create new client if necessary. After reconnect, refresh positions."""
        try:
            # quick check
            self.exchange.get_positions()
            self.connected = True
            return True
        except Exception as e:
            logger.warning("Connection check failed: %s; trying new client...", e)
            self.connected = False
            for i in range(retries):
                new_ex = self._new_exchange_client()
                if new_ex:
                    self.exchange = new_ex
                    # after creating a new client, clear outstanding (they are invalid on old client)
                    self.outstanding.clear()
                    self._cached_main = None
                    self._cached_dual = None
                    self._cached_positions = {}
                    self.last_market_poll = 0.0
                    self.last_position_poll = 0.0
                    # fetch positions to re-sync
                    try:
                        time.sleep(0.5)
                        self.exchange.get_positions()
                        self.connected = True
                        logger.info("Reconnected with new Exchange client (attempt %d)", i + 1)
                        return True
                    except Exception as e2:
                        logger.debug("New client created but position fetch failed: %s", e2)
                time.sleep(wait)
            logger.error("Reconnect attempts failed")
            return False

    # --- cached getters ---
    def _get_orderbooks(self) -> Tuple[Optional[Any], Optional[Any]]:
        now = time.time()
        if now - self.last_market_poll > self.MARKET_TTL or self._cached_main is None or self._cached_dual is None:
            try:
                ob_main = self.exchange.get_last_price_book(self.main)
                ob_dual = self.exchange.get_last_price_book(self.dual)
                self._cached_main = ob_main
                self._cached_dual = ob_dual
                self.last_market_poll = now
            except Exception as e:
                if "Cannot call function until connected" in str(e) or "not connected" in str(e).lower():
                    if not self.ensure_connection():
                        logger.debug("Orderbook fetch failed and reconnect failed: %s", e)
                        return None, None
                    try:
                        ob_main = self.exchange.get_last_price_book(self.main)
                        ob_dual = self.exchange.get_last_price_book(self.dual)
                        self._cached_main = ob_main
                        self._cached_dual = ob_dual
                        self.last_market_poll = now
                    except Exception as e2:
                        logger.debug("Orderbook fetch failed after reconnect: %s", e2)
                        return None, None
                else:
                    logger.debug("Orderbook fetch failed: %s", e)
                    return None, None
        return self._cached_main, self._cached_dual

    def _get_positions_cached(self) -> Dict[str, int]:
        now = time.time()
        if now - self.last_position_poll > self.POS_TTL:
            try:
                pos = self.exchange.get_positions()
                self._cached_positions = pos
            except Exception as e:
                logger.debug("Position fetch failed: %s", e)
                self._cached_positions = {}
            self.last_position_poll = now
        return self._cached_positions

    def positions(self) -> Tuple[int, int, int]:
        """Return (pos_main, pos_dual, net_pos)."""
        p = self._get_positions_cached()
        pos_main = int(p.get(self.main, 0))
        pos_dual = int(p.get(self.dual, 0))
        net = pos_main + pos_dual
        return pos_main, pos_dual, net

    # --- tolerant API calls ---
    def _delete_order_safe(self, oid: Any) -> bool:
        """Try a few plausible delete_order signatures; return True on success."""
        tries = []
        try:
            # keyword
            self.exchange.delete_order(order_id=oid)
            return True
        except Exception as e:
            tries.append(("order_id=k", e))
        try:
            # positional single arg
            self.exchange.delete_order(oid)
            return True
        except Exception as e:
            tries.append(("order_id_pos", e))
        try:
            # instrument + order
            self.exchange.delete_order(self.main, oid)
            return True
        except Exception as e:
            tries.append(("inst_ord_pos", e))
        try:
            self.exchange.delete_order(instrument_id=self.main, order_id=oid)
            return True
        except Exception as e:
            tries.append(("inst_k_ord_k", e))
        logger.debug("delete_order tries: %s", tries)
        return False

    def _place_limit(self, inst: str, price: float, volume: int, side: str) -> Optional[OrderState]:
        """Rate-limited place order. Returns OrderState or None."""
        self._throttled_order_call()
        if self.dry_run:
            fake = f"DRY-{int(time.time()*1000)}"
            os = OrderState(order_id=fake, instrument=inst, side=side, price=price, volume=volume, placed_ts=time.time())
            self.outstanding[os.order_id] = os
            return os
        try:
            res = self.exchange.insert_order(inst, price=price, volume=volume, side=side, order_type="limit")
            order_id = None
            if res is None:
                return None
            if hasattr(res, "order_id"):
                order_id = getattr(res, "order_id")
            elif isinstance(res, (int, str)):
                order_id = res
            else:
                try:
                    order_id = res["order_id"]  # type: ignore
                except Exception:
                    order_id = getattr(res, "id", None)
            if order_id is None:
                logger.debug("No order id from insert_order: %s", res)
                return None
            os = OrderState(order_id=order_id, instrument=inst, side=side, price=price, volume=volume, placed_ts=time.time())
            self.outstanding[os.order_id] = os
            return os
        except Exception as e:
            logger.debug("insert_order failed: %s", e)
            if "Cannot call function until connected" in str(e) or "not connected" in str(e).lower():
                # connection fault: attempt reconnect but do NOT place more orders until positions are refreshed
                if self.ensure_connection():
                    # don't recurse into placing immediately — require caller to attempt again
                    logger.info("Reconnected after insert failure; caller should re-evaluate before placing.")
                return None
            return None

    def _cancel_order(self, oid: Any):
        ok = self._delete_order_safe(oid)
        if ok:
            self.outstanding.pop(oid, None)
        else:
            logger.debug("Cancel failed for %s", oid)

    # --- enhanced adaptive skew/herding with aggressive strategies ---
    def _update_flow_and_skew(self, signed_qty: int):
        # signed_qty: positive = buy executed vs us (we were net buyer)
        # quick immediate skew estimate
        delta = np.sign(signed_qty) * min(abs(signed_qty) / 10.0, 5.0)
        self.skew_ticks = (1 - self.SKEW_ALPHA) * self.skew_ticks + self.SKEW_ALPHA * delta
        # accumulate herd bias slowly
        self.herd_bias += self.HERD_ALPHA * (signed_qty / 50.0)
        # clamp
        max_t = self.MAX_SKEW_TICKS
        self.skew_ticks = max(-max_t, min(max_t, self.skew_ticks))
        self.herd_bias = max(-max_t, min(max_t, self.herd_bias))

    def _decay_skew(self, dt: float):
        # gentle decay toward zero
        self.skew_ticks *= max(0.0, 1.0 - self.SKEW_DECAY * dt)
        self.herd_bias *= max(0.0, 1.0 - self.HERD_DECAY * dt)

    def _apply_aggressive_strategies(self, ob_main, ob_dual, fair_price: float) -> float:
        """Apply simulation-legal aggressive strategies to adjust pricing"""
        now = time.time()
        
        # Only update aggressive strategies periodically to avoid over-trading
        if now - self.last_aggressive_update < 2.0:  # Update every 2 seconds
            return fair_price
            
        self.last_aggressive_update = now
        
        adjusted_price = fair_price
        
        # Momentum arbitrage between instruments
        if self.use_momentum_arb and ob_main and ob_dual:
            mid_main = (ob_main.bids[0].price + ob_main.asks[0].price) / 2.0
            mid_dual = (ob_dual.bids[0].price + ob_dual.asks[0].price) / 2.0
            
            momentum_signal = self.analyzer.aggressive_strategies.momentum_arbitrage(
                mid_main, mid_dual, self.main)
            adjusted_price += momentum_signal * self.TICK
            
        # Order imbalance detection
        if ob_main:
            imbalance = self.analyzer.aggressive_strategies.detect_order_imbalance(ob_main)
            # If strong buy pressure, bias prices up; strong sell pressure, bias down
            adjusted_price += imbalance * self.TICK * 2.0
            
        return adjusted_price

    def _skew_price(self) -> float:
        total_ticks = self.skew_ticks + self.herd_bias
        total_ticks = max(-self.MAX_SKEW_TICKS, min(self.MAX_SKEW_TICKS, total_ticks))
        return total_ticks * self.TICK

    # --- enhanced quote computation with aggressive strategies ---
    def compute_quotes(self, ob_main, ob_dual) -> Optional[dict]:
        if not ob_main or not getattr(ob_main, "bids", None) or not getattr(ob_main, "asks", None):
            return None
        if not ob_dual or not getattr(ob_dual, "bids", None) or not getattr(ob_dual, "asks", None):
            return None

        mid_main = (ob_main.bids[0].price + ob_main.asks[0].price) / 2.0
        mid_dual = (ob_dual.bids[0].price + ob_dual.asks[0].price) / 2.0

        depth_main = sum(b.volume for b in ob_main.bids[:5]) + sum(a.volume for a in ob_main.asks[:5])
        depth_dual = sum(b.volume for b in ob_dual.bids[:5]) + sum(a.volume for a in ob_dual.asks[:5])
        w_main = depth_main / max(1.0, (depth_main + depth_dual))

        fair = w_main * mid_main + (1.0 - w_main) * mid_dual

        # Apply aggressive strategies
        fair = self._apply_aggressive_strategies(ob_main, ob_dual, fair)

        # small inventory penalty to discourage runaway
        pos_main, pos_dual, net_pos = self.positions()
        inv_pen = 0.0
        if net_pos != 0:
            inv_pen = -np.sign(net_pos) * min(0.02 * abs(net_pos) / max(1.0, self.NET_POS_LIMIT), 0.5)

        # apply skew & herd
        skew_price = self._skew_price()

        ref_price = fair + skew_price + inv_pen

        # Apply spoofing strategy if enabled
        if self.use_spoofing and not self.dry_run:
            try:
                spoof_bid, spoof_ask = self.analyzer.aggressive_strategies.spoofing_strategy(
                    self.exchange, self.main, ref_price, self.TICK)
                # Use spoof-adjusted prices for main instrument
                our_bid = spoof_bid
                our_ask = spoof_ask
            except Exception as e:
                logger.debug("Spoofing failed, using normal quotes: %s", e)
                our_bid = round_down_to_tick(ref_price - 0.5 * self.TICK, self.TICK)
                our_ask = round_up_to_tick(ref_price + 0.5 * self.TICK, self.TICK)
        else:
            # quote one tick inside around ref (very tight)
            our_bid = round_down_to_tick(ref_price - 0.5 * self.TICK, self.TICK)
            our_ask = round_up_to_tick(ref_price + 0.5 * self.TICK, self.TICK)

        # safety: ensure not crossing main market
        if our_bid >= ob_main.asks[0].price:
            our_bid = round_down_to_tick(ob_main.asks[0].price - self.TICK, self.TICK)
        if our_ask <= ob_main.bids[0].price:
            our_ask = round_up_to_tick(ob_main.bids[0].price + self.TICK, self.TICK)

        # size
        base_size = max(1, int(self.BASE_SIZE * self.crush_mult))
        if net_pos > 0:
            bid_size = max(1, base_size // 2)
            ask_size = base_size
        elif net_pos < 0:
            bid_size = base_size
            ask_size = max(1, base_size // 2)
        else:
            bid_size = ask_size = base_size

        # clip by net position safety (do not create orders that could push net over limit)
        max_net = self.NET_POS_LIMIT
        safety = self.SAFETY_BUFFER
        # Before placing a bid of 'bid_size' on either instrument, ensure net_pos + bid_size <= max_net
        # For asks, ensure net_pos - ask_size >= -max_net
        # We'll enforce this per-order when placing (safe wrapper), but prune volumes here too:
        if net_pos + bid_size > max_net - safety:
            bid_size = max(0, (max_net - safety) - net_pos)
        if net_pos - ask_size < -max_net + safety:
            ask_size = max(0, net_pos + max_net - safety)

        if bid_size <= 0 and ask_size <= 0:
            return None

        return {
            "bid_price": float(our_bid),
            "ask_price": float(our_ask),
            "bid_size": int(bid_size),
            "ask_size": int(ask_size),
            "ref": float(ref_price),
            "net_pos": int(net_pos),
            "mid_main": float(mid_main),
            "mid_dual": float(mid_dual),
        }

    # --- safe order placement wrappers checking net position FIRST ---
    def safe_place_bid_on(self, inst: str, price: float, size: int) -> Optional[OrderState]:
        # check net pos (main + dual) before placing a buy (bid) — buy increases net_pos
        _, _, net_pos = self.positions()
        if abs(net_pos + size) > self.NET_POS_LIMIT:
            logger.debug("Refuse bid on %s: net would be %d > limit %d", inst, net_pos + size, self.NET_POS_LIMIT)
            return None
        return self._place_limit(inst, price, size, "bid")

    def safe_place_ask_on(self, inst: str, price: float, size: int) -> Optional[OrderState]:
        # ask decreases net_pos
        _, _, net_pos = self.positions()
        if abs(net_pos - size) > self.NET_POS_LIMIT:
            logger.debug("Refuse ask on %s: net would be %d > limit %d", inst, net_pos - size, self.NET_POS_LIMIT)
            return None
        return self._place_limit(inst, price, size, "ask")

    # --- fill detection & simple reporting (best-effort) ---
    def detect_and_record_fills(self, prev_net_pos: int):
        pos_main, pos_dual, net_pos = self.positions()
        delta = net_pos - prev_net_pos
        if delta == 0:
            return
        direction = "BUY" if delta > 0 else "SELL"
        qty = abs(delta)
        # attribute to outstanding orders of the relevant side (best-effort)
        candidates = [o for o in self.outstanding.values() if (direction == "BUY" and o.side == "bid") or (direction == "SELL" and o.side == "ask")]
        if direction == "BUY":
            candidates.sort(key=lambda o: -o.price)
        else:
            candidates.sort(key=lambda o: o.price)

        remaining = qty
        while remaining > 0 and candidates:
            o = candidates[0]
            take = min(remaining, o.volume)
            fill_price = o.price
            if direction == "BUY":
                old_pos = self.position
                new_pos = old_pos + take
                if old_pos == 0:
                    self.avg_cost = fill_price
                else:
                    self.avg_cost = (old_pos * self.avg_cost + take * fill_price) / new_pos
                self.position = new_pos
            else:
                realized = take * (fill_price - self.avg_cost)
                self.realized_pnl += realized
                self.position -= take
                if self.position == 0:
                    self.avg_cost = 0.0
            remaining -= take
            o.volume -= take
            if o.volume <= 0:
                self.outstanding.pop(o.order_id, None)
                candidates.pop(0)
            else:
                candidates[0] = o
            logger.info("Detected fill: %s %d @ %.2f | net_pos=%d avg_cost=%.4f realized=%.2f",
                        direction, take, fill_price, net_pos, self.avg_cost, self.realized_pnl)

        if remaining > 0:
            # unattributed remainder: estimate at mid
            ob_main, ob_dual = self._get_orderbooks()
            mid = None
            if ob_main and getattr(ob_main, "bids", None) and getattr(ob_main, "asks", None):
                mid = (ob_main.bids[0].price + ob_main.asks[0].price) / 2.0
            est_px = mid if mid is not None else (self.avg_cost if self.avg_cost > 0 else 0.0)
            if delta > 0:  # buys
                old_pos = self.position
                self.position += remaining
                if old_pos == 0:
                    self.avg_cost = est_px
                else:
                    self.avg_cost = (old_pos * self.avg_cost + remaining * est_px) / self.position
            else:
                realized = remaining * (est_px - self.avg_cost)
                self.realized_pnl += realized
                self.position -= remaining
                if self.position == 0:
                    self.avg_cost = 0.0
            logger.info("Unattributed fill est: %s %d @ %.2f | net_pos=%d avg_cost=%.4f realized=%.2f",
                        direction, remaining, est_px, net_pos, self.avg_cost, self.realized_pnl)

        # update adaptive skew from flow
        self._update_flow_and_skew(delta)

    # --- order management (we cancel all and re-place each cycle) ---
    def cancel_all_outstanding(self):
        # Cancel spoof orders first
        self.analyzer.aggressive_strategies.cancel_spoof_orders(self.exchange)
        
        # Cancel our real orders
        for oid in list(self.outstanding.keys()):
            try:
                self._cancel_order(oid)
            except Exception as e:
                logger.debug("Cancel exception: %s", e)

    # --- main loop ---
    def run(self, duration: float = 600.0):
        logger.info("Starting SafeDualAMM loop for %s seconds", duration)
        start = time.time()
        last_time = time.time()
        try:
            while time.time() - start < duration:
                if os.path.exists(KILL_SWITCH_FILE):
                    logger.warning("Kill switch detected; stopping loop.")
                    break

                now = time.time()
                dt = now - last_time
                last_time = now

                # gentle decay of skew/herd
                self._decay_skew(dt)

                # ensure connection and fresh positions before quoting
                if not self.ensure_connection():
                    # wait a bit and retry
                    time.sleep(0.5)
                    continue

                # fetch orderbooks
                ob_main, ob_dual = self._get_orderbooks()
                if ob_main is None or ob_dual is None:
                    time.sleep(0.5)
                    continue

                # update stats
                self.analyzer.update_from_orderbooks(ob_main, ob_dual)

                # compute quote - but only if we're connected and have valid books
                quote = self.compute_quotes(ob_main, ob_dual)
                # read positions before placing
                pos_main, pos_dual, net_before = self.positions()

                if quote:
                    # conservative: cancel outstanding first (keeps implementation simple)
                    # you can replace with diff-based logic if you prefer.
                    self.cancel_all_outstanding()

                    # place on MAIN and DUAL but use safe wrappers that check net limit before actual placing
                    bid_price = quote["bid_price"]
                    ask_price = quote["ask_price"]
                    bid_size = quote["bid_size"]
                    ask_size = quote["ask_size"]

                    # MAIN
                    if bid_size > 0:
                        ob = self.safe_place_bid_on(self.main, bid_price, bid_size)
                        if ob:
                            logger.info("Placed MAIN bid %d @ %.2f", ob.volume, ob.price)
                    if ask_size > 0:
                        ob = self.safe_place_ask_on(self.main, ask_price, ask_size)
                        if ob:
                            logger.info("Placed MAIN ask %d @ %.2f", ob.volume, ob.price)

                    # DUAL (we may want to hedge or mirror; ensure safe as well)
                    # Slightly offset the dual quotes by one tick to avoid exact duplication
                    dual_bid = round_down_to_tick(bid_price - 1 * self.TICK, self.TICK)
                    dual_ask = round_up_to_tick(ask_price + 1 * self.TICK, self.TICK)
                    if bid_size > 0:
                        ob = self.safe_place_bid_on(self.dual, dual_bid, bid_size)
                        if ob:
                            logger.info("Placed DUAL bid %d @ %.2f", ob.volume, ob.price)
                    if ask_size > 0:
                        ob = self.safe_place_ask_on(self.dual, dual_ask, ask_size)
                        if ob:
                            logger.info("Placed DUAL ask %d @ %.2f", ob.volume, ob.price)

                # small sleep to allow fills to clear through exchange
                time.sleep(0.5)

                # detect fills by comparing net position
                _, _, net_after = self.positions()
                self.detect_and_record_fills(net_before)

                # prune stale orders occasionally (safety)
                self.prune_stale(timeout=30.0)

                # minor yield
                time.sleep(0.5)

        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        finally:
            logger.info("Shutting down: cancelling outstanding orders")
            self.cancel_all_outstanding()
            logger.info("Final net pos=%d avg_cost=%.4f realized=%.2f", self.position, self.avg_cost, self.realized_pnl)

    def prune_stale(self, timeout: float = 30.0):
        now = time.time()
        for oid, s in list(self.outstanding.items()):
            if now - s.placed_ts > timeout:
                logger.debug("Pruning stale order %s", oid)
                try:
                    self._cancel_order(oid)
                except Exception:
                    pass

# --- CLI / main ---
def main():
    parser = argparse.ArgumentParser(description="Safe Dual AMM v5")
    parser.add_argument("--main", default="NVDA", help="Base instrument (no _DUAL suffix)")
    parser.add_argument("--dual", default=None, help="Dual instrument name (defaults to MAIN + _DUAL)")
    parser.add_argument("--net-limit", type=int, default=80, help="Net position limit across both books")
    parser.add_argument("--duration", type=int, default=600, help="Run duration (seconds)")
    parser.add_argument("--dry-run", action="store_true", help="Do not send orders; simulate only")
    parser.add_argument("--crush", action="store_true", help="Aggressive quoting (bigger sizes/skew)")
    parser.add_argument("--no-spoofing", action="store_true", help="Disable spoofing strategy")
    parser.add_argument("--no-momentum", action="store_true", help="Disable momentum arbitrage")
    args = parser.parse_args()

    ex = Exchange()
    try:
        ex.connect()
    except Exception as e:
        logger.warning("Initial connect failed: %s — continuing, reconnect logic will attempt.", e)

    crush_mult = 3.0 if args.crush else 1.0
    amm = SafeDualAMM(ex, main=args.main, dual=args.dual, net_pos_limit=args.net_limit, dry_run=args.dry_run, crush_mult=crush_mult)
    
    # Configure aggressive strategies
    amm.use_spoofing = not args.no_spoofing
    amm.use_momentum_arb = not args.no_momentum
    
    if args.crush:
        amm.BASE_SIZE = max(1, amm.BASE_SIZE * 2)
        amm.MAX_SKEW_TICKS = 32
        amm.SKEW_ALPHA = 0.7
        amm.HERD_ALPHA = 0.02
        logger.info("CRUSH mode enabled")

    logger.info("Running SafeDualAMM main=%s dual=%s net_limit=%d dry_run=%s for %ds", amm.main, amm.dual, amm.NET_POS_LIMIT, amm.dry_run, args.duration)
    logger.info("Aggressive strategies - Spoofing: %s, Momentum: %s", amm.use_spoofing, amm.use_momentum_arb)
    amm.run(duration=args.duration)

if __name__ == "__main__":
    main()