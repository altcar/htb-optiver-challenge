#!/usr/bin/env python3
"""
Market Making Bot with Arbitrage Detection
Features:
- Clears all positions at startup (loop until fully flat)
- Tracks internal net positions to avoid exceeding limits
- Adjusts orders if positions exceed limits
- Combines market making and dual arbitrage
"""
import time
import logging
import argparse
from typing import Dict, Optional
import numpy as np
from optibook.synchronous_client import Exchange

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("simple_mm")
logging.getLogger("client").setLevel("ERROR")

# --- Parameters ---
QUOTED_VOLUME = 5
TICK_SIZE = 0.10
POSITION_LIMIT = 75
MIN_SPREAD = 0.01
MAX_INVENTORY = 50
ARB_THRESHOLD = 0.15  # Minimum profit to execute arbitrage
ARB_VOLUME = 50       # Volume per arbitrage leg


def round_price(price: float, tick: float, direction: str) -> float:
    if direction == "down":
        return float(np.floor(price / tick) * tick)
    return float(np.ceil(price / tick) * tick)


class ArbitrageMarketMaker:
    def __init__(self, exchange: Exchange):
        self.exchange = exchange
        self.connected = False
        self.instruments: Dict[str, dict] = {}
        self.active_orders: Dict[str, dict] = {}
        self.dual_pairs: Dict[str, str] = {}
        self.arb_count = 0
        self.net_positions: Dict[str, int] = {}

    # -------------------- Connection --------------------
    def connect(self) -> bool:
        try:
            if not self.connected:
                self.exchange.connect()
                time.sleep(0.1)
            self.connected = True
            logger.info("Connected to exchange")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.connected = False
            return False

    # -------------------- Initialization --------------------
    def initialize_instruments(self):
        instruments = self.exchange.get_instruments()
        for inst_id in instruments:
            self.instruments[inst_id] = {"position": 0}
            self.active_orders[inst_id] = {"bid": None, "ask": None, "bid_price": None, "ask_price": None}
            self.net_positions[inst_id] = 0
            if inst_id.endswith("_DUAL"):
                base = inst_id.replace("_DUAL", "")
                if base in instruments:
                    self.dual_pairs[base] = inst_id
                    self.dual_pairs[inst_id] = base
        logger.info(f"Initialized {len(self.instruments)} instruments, {len(self.dual_pairs)//2} DUAL pairs")

    # -------------------- Safe API Calls --------------------
    def safe_call(self, func, *args, **kwargs):
        if not self.connected:
            if not self.connect():
                return None
        try:
            return func(*args, **kwargs)
        except Exception as e:
            error_msg = str(e).lower()
            if "not connected" in error_msg or "cannot call function" in error_msg:
                logger.warning("Disconnected, attempting reconnect...")
                self.connected = False
                time.sleep(0.1)
                if self.connect():
                    try:
                        return func(*args, **kwargs)
                    except:
                        pass
            return None

    # -------------------- Position Management --------------------
    def get_positions(self) -> Dict[str, int]:
        result = self.safe_call(self.exchange.get_positions)
        return {k: int(v) for k, v in result.items()} if result else {}

    def flatten_positions(self):
        """Cancel all orders and flatten current positions at startup"""
        logger.info("Flattening all positions at startup...")
        
        # Cancel all existing orders
        for inst_id in self.instruments:
            try:
                self.safe_call(self.exchange.delete_orders, inst_id)
            except:
                pass
        
        time.sleep(0.1)  # Let cancellations process
        
        # Get current positions
        positions = self.get_positions()
        
        for inst_id, pos in positions.items():
            if pos == 0:
                continue
                
            attempts = 0
            max_attempts = 5
            
            while pos != 0 and attempts < max_attempts:
                attempts += 1
                
                # Get orderbook to determine price
                ob = self.safe_call(self.exchange.get_last_price_book, inst_id)
                if not ob or not ob.bids or not ob.asks:
                    logger.warning(f"No orderbook for {inst_id}, skipping")
                    break
                
                # Determine side and price
                if pos > 0:
                    # Need to sell
                    side = 'ask'
                    price = ob.bids[0].price  # Hit the bid
                    volume = min(abs(pos), 10)
                else:
                    # Need to buy
                    side = 'bid'
                    price = ob.asks[0].price  # Hit the ask
                    volume = min(abs(pos), 10)
                
                try:
                    logger.info(f"Flattening {inst_id}: {side} {volume} @ {price} (current pos: {pos})")
                    self.safe_call(
                        self.exchange.insert_order,
                        inst_id,
                        price=price,
                        volume=volume,
                        side=side,
                        order_type='ioc'
                    )
                    time.sleep(0.1)
                    
                    # Update position
                    current_positions = self.get_positions()
                    pos = current_positions.get(inst_id, 0)
                    self.net_positions[inst_id] = pos
                    
                except Exception as e:
                    logger.error(f"Failed to flatten {inst_id}: {e}")
                    break
        
        # Final position check
        final_positions = self.get_positions()
        total_pos = sum(abs(v) for v in final_positions.values())
        logger.info(f"Flattening complete. Total |position|: {total_pos}")

    def update_net_position(self, inst_id: str, side: str, volume: int):
        """Update internal position tracking"""
        if side == 'bid':
            self.net_positions[inst_id] += volume
        else:
            self.net_positions[inst_id] -= volume

    # -------------------- Arbitrage --------------------
    def check_arbitrage(self, base_inst: str) -> Optional[Dict]:
        if base_inst not in self.dual_pairs:
            return None
        dual_inst = self.dual_pairs[base_inst]
        base_ob = self.safe_call(self.exchange.get_last_price_book, base_inst)
        dual_ob = self.safe_call(self.exchange.get_last_price_book, dual_inst)
        if not (base_ob and dual_ob): 
            return None
        if not (base_ob.bids and base_ob.asks and dual_ob.bids and dual_ob.asks): 
            return None

        base_bid, base_ask = base_ob.bids[0].price, base_ob.asks[0].price
        dual_bid, dual_ask = dual_ob.bids[0].price, dual_ob.asks[0].price
        base_bid_vol, base_ask_vol = base_ob.bids[0].volume, base_ob.asks[0].volume
        dual_bid_vol, dual_ask_vol = dual_ob.bids[0].volume, dual_ob.asks[0].volume

        # Use internal net positions
        base_pos = self.net_positions.get(base_inst, 0)
        dual_pos = self.net_positions.get(dual_inst, 0)

        # Strategy 1: buy base, sell dual
        profit_1 = dual_bid - base_ask
        can_do_1 = (base_pos < POSITION_LIMIT - 10) and (dual_pos > -POSITION_LIMIT + 10)

        # Strategy 2: sell base, buy dual
        profit_2 = base_bid - dual_ask
        can_do_2 = (base_pos > -POSITION_LIMIT + 10) and (dual_pos < POSITION_LIMIT - 10)

        if profit_1 > ARB_THRESHOLD and can_do_1:
            volume = min(ARB_VOLUME, base_ask_vol, dual_bid_vol,
                         POSITION_LIMIT - 10 - base_pos, 
                         POSITION_LIMIT - 10 + dual_pos)
            if volume > 0:
                return {"type": "buy_base_sell_dual", "base_inst": base_inst,
                        "dual_inst": dual_inst, "buy_price": base_ask,
                        "sell_price": dual_bid, "volume": volume,
                        "profit": profit_1 * volume}

        if profit_2 > ARB_THRESHOLD and can_do_2:
            volume = min(ARB_VOLUME, base_bid_vol, dual_ask_vol,
                         POSITION_LIMIT - 10 + base_pos, 
                         POSITION_LIMIT - 10 - dual_pos)
            if volume > 0:
                return {"type": "sell_base_buy_dual", "base_inst": base_inst,
                        "dual_inst": dual_inst, "sell_price": base_bid,
                        "buy_price": dual_ask, "volume": volume,
                        "profit": profit_2 * volume}

        return None

    def execute_arbitrage(self, arb: Dict):
        try:
            self.arb_count += 1
            logger.info(f"🚀 ARB #{self.arb_count}: {arb['type']} - Expected profit: {arb['profit']:.2f}")

            if arb['type'] == "buy_base_sell_dual":
                self.safe_call(self.exchange.insert_order, arb['base_inst'], 
                              price=arb['buy_price'], volume=arb['volume'], 
                              side='bid', order_type='ioc')
                self.update_net_position(arb['base_inst'], 'bid', arb['volume'])
                time.sleep(0.05)
                
                self.safe_call(self.exchange.insert_order, arb['dual_inst'], 
                              price=arb['sell_price'], volume=arb['volume'], 
                              side='ask', order_type='ioc')
                self.update_net_position(arb['dual_inst'], 'ask', arb['volume'])
                
            elif arb['type'] == "sell_base_buy_dual":
                self.safe_call(self.exchange.insert_order, arb['base_inst'], 
                              price=arb['sell_price'], volume=arb['volume'], 
                              side='ask', order_type='ioc')
                self.update_net_position(arb['base_inst'], 'ask', arb['volume'])
                time.sleep(0.05)
                
                self.safe_call(self.exchange.insert_order, arb['dual_inst'], 
                              price=arb['buy_price'], volume=arb['volume'], 
                              side='bid', order_type='ioc')
                self.update_net_position(arb['dual_inst'], 'bid', arb['volume'])

            logger.info(f"✅ ARB #{self.arb_count} executed")
        except Exception as e:
            logger.error(f"❌ Arbitrage execution failed: {e}")

    # -------------------- Market Making --------------------
    def calculate_quotes(self, inst_id: str, orderbook) -> Optional[Dict]:
        if not (orderbook and orderbook.bids and orderbook.asks): 
            return None

        mid = (orderbook.bids[0].price + orderbook.asks[0].price) / 2
        our_bid = round_price(mid - MIN_SPREAD / 2, TICK_SIZE, "down")
        our_ask = round_price(mid + MIN_SPREAD / 2, TICK_SIZE, "up")

        # Ensure minimum spread
        if our_ask - our_bid < MIN_SPREAD:
            our_bid = round_price(mid - MIN_SPREAD / 2, TICK_SIZE, "down")
            our_ask = round_price(mid + MIN_SPREAD / 2, TICK_SIZE, "up")

        # Adjust volumes based on position limits
        position = self.net_positions.get(inst_id, 0)
        max_bid_vol = max(0, POSITION_LIMIT - 10 - position)
        max_ask_vol = max(0, POSITION_LIMIT - 10 + position)

        bid_vol = min(QUOTED_VOLUME, max_bid_vol)
        ask_vol = min(QUOTED_VOLUME, max_ask_vol)

        # Stop quoting if position is too large
        if position > MAX_INVENTORY:
            bid_vol = 0
            ask_vol = min(ask_vol, QUOTED_VOLUME * 2)
        elif position < -MAX_INVENTORY:
            ask_vol = 0
            bid_vol = min(bid_vol, QUOTED_VOLUME * 2)

        if bid_vol == 0 and ask_vol == 0: 
            return None
            
        return {
            "bid_price": float(our_bid), 
            "ask_price": float(our_ask), 
            "bid_volume": int(bid_vol), 
            "ask_volume": int(ask_vol)
        }

    def cancel_order(self, order_id):
        if not order_id: 
            return
        try:
            self.safe_call(self.exchange.delete_order, order_id)
        except:
            pass

    def place_order(self, inst_id: str, price: float, volume: int, side: str):
        if volume <= 0: 
            return None
        try:
            result = self.safe_call(self.exchange.insert_order, inst_id, 
                                   price=price, volume=volume, 
                                   side=side, order_type='limit')
            if result:
                # Don't update position yet - only when filled
                return result.order_id if hasattr(result, 'order_id') else result
        except Exception as e:
            logger.debug(f"Order failed for {inst_id} {side}@{price}: {e}")
        return None

    def update_instrument(self, inst_id: str):
        try:
            orderbook = self.safe_call(self.exchange.get_last_price_book, inst_id)
            if not orderbook: 
                return
                
            quotes = self.calculate_quotes(inst_id, orderbook)
            active = self.active_orders[inst_id]

            if not quotes:
                self.cancel_order(active["bid"])
                self.cancel_order(active["ask"])
                self.active_orders[inst_id] = {"bid": None, "ask": None, "bid_price": None, "ask_price": None}
                return

            # Update bid
            need_bid_update = (
                active["bid"] is None or 
                active["bid_price"] is None or 
                abs(active["bid_price"] - quotes["bid_price"]) > 0.001
            )
            
            if need_bid_update and quotes["bid_volume"] > 0:
                self.cancel_order(active["bid"])
                active["bid"] = self.place_order(inst_id, quotes["bid_price"], quotes["bid_volume"], "bid")
                active["bid_price"] = quotes["bid_price"] if active["bid"] else None
            elif quotes["bid_volume"] == 0:
                self.cancel_order(active["bid"])
                active["bid"] = None
                active["bid_price"] = None

            # Update ask
            need_ask_update = (
                active["ask"] is None or 
                active["ask_price"] is None or 
                abs(active["ask_price"] - quotes["ask_price"]) > 0.001
            )
            
            if need_ask_update and quotes["ask_volume"] > 0:
                self.cancel_order(active["ask"])
                active["ask"] = self.place_order(inst_id, quotes["ask_price"], quotes["ask_volume"], "ask")
                active["ask_price"] = quotes["ask_price"] if active["ask"] else None
            elif quotes["ask_volume"] == 0:
                self.cancel_order(active["ask"])
                active["ask"] = None
                active["ask_price"] = None
                
        except Exception as e:
            logger.error(f"Error updating {inst_id}: {e}")

    # -------------------- Execution Loops --------------------
    def scan_and_execute_arbitrage(self):
        for base_inst in [inst for inst in self.instruments if not inst.endswith("_DUAL")]:
            arb = self.check_arbitrage(base_inst)
            if arb:
                self.execute_arbitrage(arb)
                time.sleep(0.1)

    def sync_positions(self):
        """Periodically sync internal positions with exchange"""
        positions = self.get_positions()
        for inst_id in self.instruments:
            self.net_positions[inst_id] = positions.get(inst_id, 0)

    def print_status(self):
        try:
            pos_str = " ".join([f"{k}:{v:+d}" for k, v in sorted(self.net_positions.items()) if v != 0])
            if not pos_str: 
                pos_str = "FLAT"
            active_count = sum(1 for inst_id in self.active_orders 
                             if self.active_orders[inst_id]["bid"] or self.active_orders[inst_id]["ask"])
            print(f"Active: {active_count}/{len(self.instruments)} | Arbs: {self.arb_count} | Pos: {pos_str}")
        except Exception as e:
            logger.debug(f"Status print failed: {e}")

    def cleanup(self):
        logger.info("Cleaning up all orders...")
        for inst_id in self.instruments:
            try:
                self.safe_call(self.exchange.delete_orders, inst_id)
            except:
                pass

    def run(self, duration: float = 600.0, update_interval: float = 1.0):
        logger.info("Starting trading bot...")
        self.flatten_positions()
        
        start = time.time()
        iteration = 0
        
        try:
            while time.time() - start < duration:
                iteration += 1
                print(f"\n--- Iteration {iteration} ---")
                
                if not self.connected and not self.connect():
                    time.sleep(1)
                    continue
                
                # Sync positions every 10 iterations
                if iteration % 10 == 0:
                    self.sync_positions()
                
                # Check for arbitrage first
                self.scan_and_execute_arbitrage()
                
                # Update market making quotes
                for inst_id in self.instruments:
                    self.update_instrument(inst_id)
                    time.sleep(0.08)
                
                self.print_status()
                time.sleep(update_interval)
                
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.cleanup()
            logger.info(f"Shutdown complete - Total arbitrages executed: {self.arb_count}")


# -------------------- Main --------------------
def main():
    parser = argparse.ArgumentParser(description="Arbitrage Market Maker")
    parser.add_argument("--duration", type=int, default=600, help="Run duration in seconds")
    parser.add_argument("--interval", type=float, default=0.10, help="Update interval")
    args = parser.parse_args()

    logger.info("Connecting to exchange...")
    exchange = Exchange()
    mm = ArbitrageMarketMaker(exchange)
    
    if not mm.connect():
        logger.error("Failed to connect to exchange")
        return
    
    mm.initialize_instruments()
    mm.run(duration=args.duration, update_interval=args.interval)


if __name__ == "__main__":
    main()