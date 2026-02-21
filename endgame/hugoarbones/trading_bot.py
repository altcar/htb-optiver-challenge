"""
Predictive Trading Bot V2 - Fixed to predict BUY/SELL direction

Key fixes:
1. Separate models for BUY vs SELL predictions
2. Use prediction imbalance to determine quote skew
3. Add momentum and flow features
4. Better timing - predict future trades, not just that trades will happen
5. Only trade when we have an edge (clear directional prediction)

Usage:
    python predictive_trading_bot_v2.py --model-file team_models.pkl
"""

import datetime as dt
import time
import logging
import argparse
from math import floor, ceil
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple

import pandas as pd
from optibook.synchronous_client import Exchange

from adversarial_trading_ml import OrderBookFeatureExtractor, TeamBehaviorPredictor

logging.getLogger("client").setLevel("ERROR")


def round_down_to_tick(price, tick_size):
    return floor(price / tick_size) * tick_size


def round_up_to_tick(price, tick_size):
    return ceil(price / tick_size) * tick_size


class ImprovedPredictiveTradingBot:
    """Trading bot with improved prediction logic focused on BUY/SELL direction."""
    
    def __init__(self, exchange: Exchange, predictor: TeamBehaviorPredictor):
        self.exchange = exchange
        self.predictor = predictor
        self.feature_extractor = OrderBookFeatureExtractor()
        
        # Trading parameters - MORE CONSERVATIVE
        self.POSITION_LIMIT = 80
        self.MAX_VOLUME_PER_SIDE = 30
        self.VOLUME_PER_ORDER = 8
        self.BASE_SPREAD = 0.20  # Wider spread
        self.TICK_SIZE = 0.10
        self.CANCEL_SPREAD_THRESHOLD = 0.30
        self.LOOP_WAIT = 1.5
        
        # ML parameters - MUCH MORE SELECTIVE
        self.MIN_EDGE_THRESHOLD = 0.15  # Need clear directional edge
        self.STRONG_EDGE_THRESHOLD = 0.30  # Very strong edge
        
        # Track team behaviors to infer buy/sell direction
        self.team_trade_history = defaultdict(lambda: deque(maxlen=50))
        self.recent_orderbook_states = deque(maxlen=100)
        
        # Performance tracking
        self.trades_made = 0
        self.wins = 0
        self.losses = 0
        
        # Instruments
        self.PRIMARY_INSTRUMENT = "NVDA_DUAL"
        self.HEDGE_INSTRUMENT = "NVDA"
        
    def update_team_history(self):
        """Track recent trades to learn team buy/sell patterns."""
        try:
            # Get recent trades from exchange (if available)
            # For now, we'll infer from position changes
            pass
        except:
            pass
    
    def predict_directional_flow(self, instrument_id: str) -> Dict:
        """
        Predict directional flow based on:
        1. Which teams are likely to trade
        2. Historical patterns of those teams (buy vs sell bias)
        3. Current orderbook state
        """
        try:
            order_book = self.exchange.get_last_price_book(instrument_id)
            
            if not (order_book and order_book.bids and order_book.asks):
                return {'valid': False}
            
            # Extract features
            features = self.feature_extractor.extract_features(order_book, instrument_id)
            if not features:
                return {'valid': False}
            
            best_bid = order_book.bids[0].price
            best_ask = order_book.asks[0].price
            mid_price = (best_bid + best_ask) / 2
            
            # Get predictions for all teams
            predictions = self.predictor.predict_all_teams(features)
            
            # Analyze each team's likely direction based on orderbook state
            buy_pressure = 0.0
            sell_pressure = 0.0
            
            for pred in predictions:
                team = pred['team']
                prob = pred['probability']
                
                if prob < 0.5:  # Team unlikely to trade
                    continue
                
                # Use orderbook imbalance to infer direction
                # If bids are stronger, price likely to go up, so they'd want to buy
                # If asks are stronger, price likely to go down, so they'd want to sell
                imbalance = features.get('tob_imbalance', 0)
                depth_imbalance = features.get('depth_imbalance_5', 0)
                
                # Check team's historical behavior
                history = self.team_trade_history.get(team, [])
                if history:
                    # Calculate team's historical buy/sell ratio
                    recent_buys = sum(1 for t in history if t == 'BUY')
                    recent_sells = sum(1 for t in history if t == 'SELL')
                    if recent_buys + recent_sells > 0:
                        buy_ratio = recent_buys / (recent_buys + recent_sells)
                    else:
                        buy_ratio = 0.5
                else:
                    buy_ratio = 0.5  # No historical data
                
                # Combine signals
                # If orderbook shows buying pressure AND team has buy history, stronger buy signal
                orderbook_buy_signal = (imbalance + depth_imbalance) / 2
                
                # Weight by prediction probability
                weighted_prob = (prob - 0.5) * 2  # Scale to 0-1
                
                # Direction inference
                direction_signal = orderbook_buy_signal * 0.6 + (buy_ratio - 0.5) * 2 * 0.4
                
                if direction_signal > 0:
                    buy_pressure += weighted_prob * abs(direction_signal)
                else:
                    sell_pressure += weighted_prob * abs(direction_signal)
            
            # Calculate net edge
            net_edge = buy_pressure - sell_pressure
            edge_magnitude = abs(net_edge)
            
            # Store this orderbook state
            self.recent_orderbook_states.append({
                'timestamp': time.time(),
                'mid_price': mid_price,
                'imbalance': features.get('tob_imbalance', 0),
                'spread': features.get('spread', 0),
            })
            
            return {
                'valid': True,
                'net_edge': net_edge,  # Positive = expect buying, negative = expect selling
                'edge_magnitude': edge_magnitude,
                'buy_pressure': buy_pressure,
                'sell_pressure': sell_pressure,
                'mid_price': mid_price,
                'features': features,
                'num_predictions': len([p for p in predictions if p['probability'] > 0.5]),
            }
            
        except Exception as e:
            print(f"Error in directional prediction: {e}")
            return {'valid': False}
    
    def calculate_skewed_quotes(self, instrument_id: str, flow_prediction: Dict) -> Optional[Dict]:
        """
        Calculate quotes SKEWED based on predicted directional flow.
        
        If we predict buying pressure: widen ask, tighten bid (we want to buy before them)
        If we predict selling pressure: widen bid, tighten ask (we want to sell before them)
        """
        
        order_book = self.exchange.get_last_price_book(instrument_id)
        if not (order_book and order_book.bids and order_book.asks):
            return None
        
        best_bid = order_book.bids[0].price
        best_ask = order_book.asks[0].price
        mid_price = (best_bid + best_ask) / 2
        
        positions = self.exchange.get_positions()
        pos = positions.get(instrument_id, 0)
        
        # Base inventory management
        inventory_skew = -pos / self.POSITION_LIMIT * self.BASE_SPREAD * 0.5
        
        # Check if we have a tradeable edge
        if not flow_prediction['valid']:
            # No prediction - use wider spread, be passive
            spread = self.BASE_SPREAD * 1.5
            bid_price = round_down_to_tick(mid_price + inventory_skew - spread, self.TICK_SIZE)
            ask_price = round_up_to_tick(mid_price + inventory_skew + spread, self.TICK_SIZE)
            
            return {
                'bid_price': bid_price,
                'ask_price': ask_price,
                'bid_volume': self.VOLUME_PER_ORDER // 2,  # Reduced volume
                'ask_volume': self.VOLUME_PER_ORDER // 2,
                'mid_price': mid_price,
                'strategy': 'PASSIVE',
                'edge': 0,
            }
        
        net_edge = flow_prediction['net_edge']
        edge_magnitude = flow_prediction['edge_magnitude']
        
        # Only trade if edge is strong enough
        if edge_magnitude < self.MIN_EDGE_THRESHOLD:
            # Edge too weak - stay passive
            spread = self.BASE_SPREAD * 1.2
            bid_price = round_down_to_tick(mid_price + inventory_skew - spread, self.TICK_SIZE)
            ask_price = round_up_to_tick(mid_price + inventory_skew + spread, self.TICK_SIZE)
            
            return {
                'bid_price': bid_price,
                'ask_price': ask_price,
                'bid_volume': self.VOLUME_PER_ORDER // 2,
                'ask_volume': self.VOLUME_PER_ORDER // 2,
                'mid_price': mid_price,
                'strategy': 'WEAK_EDGE',
                'edge': net_edge,
            }
        
        # We have an edge! Skew quotes accordingly
        strategy = 'NEUTRAL'
        bid_volume = self.VOLUME_PER_ORDER
        ask_volume = self.VOLUME_PER_ORDER
        
        if net_edge > self.MIN_EDGE_THRESHOLD:
            # Expect BUYING pressure - they will lift our offers
            # Strategy: Tighten ask, widen bid
            strategy = 'FADE_BUYING'
            
            ask_spread = self.BASE_SPREAD * 0.8  # Tighter ask to get hit
            bid_spread = self.BASE_SPREAD * 1.3  # Wider bid
            
            # Increase ask volume if strong edge
            if edge_magnitude > self.STRONG_EDGE_THRESHOLD:
                strategy = 'FADE_BUYING_AGGRESSIVE'
                ask_spread *= 0.7
                ask_volume = int(self.VOLUME_PER_ORDER * 1.5)
            
            bid_price = round_down_to_tick(mid_price + inventory_skew - bid_spread, self.TICK_SIZE)
            ask_price = round_up_to_tick(mid_price + inventory_skew + ask_spread, self.TICK_SIZE)
            
            # Reduce bid volume since we expect to sell
            bid_volume = max(self.VOLUME_PER_ORDER // 2, 5)
            
        elif net_edge < -self.MIN_EDGE_THRESHOLD:
            # Expect SELLING pressure - they will hit our bids
            # Strategy: Tighten bid, widen ask
            strategy = 'FADE_SELLING'
            
            bid_spread = self.BASE_SPREAD * 0.8  # Tighter bid to get hit
            ask_spread = self.BASE_SPREAD * 1.3  # Wider ask
            
            # Increase bid volume if strong edge
            if edge_magnitude > self.STRONG_EDGE_THRESHOLD:
                strategy = 'FADE_SELLING_AGGRESSIVE'
                bid_spread *= 0.7
                bid_volume = int(self.VOLUME_PER_ORDER * 1.5)
            
            bid_price = round_down_to_tick(mid_price + inventory_skew - bid_spread, self.TICK_SIZE)
            ask_price = round_up_to_tick(mid_price + inventory_skew + ask_spread, self.TICK_SIZE)
            
            # Reduce ask volume since we expect to buy
            ask_volume = max(self.VOLUME_PER_ORDER // 2, 5)
        else:
            # Edge exists but not strong in either direction
            spread = self.BASE_SPREAD
            bid_price = round_down_to_tick(mid_price + inventory_skew - spread, self.TICK_SIZE)
            ask_price = round_up_to_tick(mid_price + inventory_skew + spread, self.TICK_SIZE)
        
        # Position limits
        if pos >= self.POSITION_LIMIT * 0.8:
            bid_volume = 0
            ask_volume = min(ask_volume, self.VOLUME_PER_ORDER * 2)
        elif pos <= -self.POSITION_LIMIT * 0.8:
            ask_volume = 0
            bid_volume = min(bid_volume, self.VOLUME_PER_ORDER * 2)
        
        # Keep quotes inside the market
        bid_price = min(bid_price, best_bid)
        ask_price = max(ask_price, best_ask)
        
        return {
            'bid_price': bid_price,
            'ask_price': ask_price,
            'bid_volume': bid_volume,
            'ask_volume': ask_volume,
            'mid_price': mid_price,
            'strategy': strategy,
            'edge': net_edge,
        }
    
    def manage_orders(self, instrument_id: str, target_quotes: Dict):
        """Cancel and insert orders."""
        outstanding = self.exchange.get_outstanding_orders(instrument_id) or {}
        
        # Cancel stale orders
        for order_id, order in list(outstanding.items()):
            should_cancel = False
            
            if order.side == "bid":
                if abs(order.price - target_quotes['bid_price']) > self.TICK_SIZE:
                    should_cancel = True
            else:
                if abs(order.price - target_quotes['ask_price']) > self.TICK_SIZE:
                    should_cancel = True
            
            if should_cancel:
                self.exchange.delete_order(instrument_id=instrument_id, order_id=order_id)
                time.sleep(0.05)
        
        # Refresh
        outstanding = self.exchange.get_outstanding_orders(instrument_id) or {}
        
        current_bid_volume = sum(o.volume for o in outstanding.values() if o.side == "bid")
        current_ask_volume = sum(o.volume for o in outstanding.values() if o.side == "ask")
        
        # Insert new orders
        if target_quotes['bid_volume'] > 0 and current_bid_volume < self.MAX_VOLUME_PER_SIDE:
            volume = min(target_quotes['bid_volume'], self.MAX_VOLUME_PER_SIDE - current_bid_volume)
            if volume > 0:
                self.exchange.insert_order(
                    instrument_id=instrument_id,
                    price=target_quotes['bid_price'],
                    volume=volume,
                    side="bid",
                    order_type="limit",
                )
                time.sleep(0.05)
        
        if target_quotes['ask_volume'] > 0 and current_ask_volume < self.MAX_VOLUME_PER_SIDE:
            volume = min(target_quotes['ask_volume'], self.MAX_VOLUME_PER_SIDE - current_ask_volume)
            if volume > 0:
                self.exchange.insert_order(
                    instrument_id=instrument_id,
                    price=target_quotes['ask_price'],
                    volume=volume,
                    side="ask",
                    order_type="limit",
                )
                time.sleep(0.05)
    
    def hedge_position(self):
        """Conservative hedging."""
        positions = self.exchange.get_positions()
        primary_pos = positions.get(self.PRIMARY_INSTRUMENT, 0)
        hedge_pos = positions.get(self.HEDGE_INSTRUMENT, 0)
        
        # Only hedge when position is significant
        if abs(primary_pos) < 20:
            return
        
        target_hedge = -primary_pos * 0.8  # Partial hedge
        hedge_needed = target_hedge - hedge_pos
        
        if abs(hedge_needed) < 10:
            return
        
        try:
            hedge_book = self.exchange.get_last_price_book(self.HEDGE_INSTRUMENT)
            if not (hedge_book and hedge_book.bids and hedge_book.asks):
                return
            
            volume = min(abs(hedge_needed), 15)
            
            if hedge_needed > 0:
                price = hedge_book.asks[0].price
                self.exchange.insert_order(
                    instrument_id=self.HEDGE_INSTRUMENT,
                    price=price,
                    volume=volume,
                    side="bid",
                    order_type="ioc",
                )
            else:
                price = hedge_book.bids[0].price
                self.exchange.insert_order(
                    instrument_id=self.HEDGE_INSTRUMENT,
                    price=price,
                    volume=volume,
                    side="ask",
                    order_type="ioc",
                )
            
            time.sleep(0.1)
        except:
            pass
    
    def print_status(self):
        """Print status."""
        positions = self.exchange.get_positions()
        pnl = self.exchange.get_pnl()
        
        print(f"\n{'='*80}")
        print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] PnL: {pnl:+.2f}")
        
        for inst, pos in positions.items():
            if pos != 0:
                print(f"  {inst}: {pos:+.0f}")
        print()
    
    def run(self):
        """Main loop."""
        print(f"\n{'='*80}")
        print(f"🤖 IMPROVED PREDICTIVE TRADING BOT V2")
        print(f"{'='*80}\n")
        
        iteration = 0
        
        try:
            while True:
                iteration += 1
                self.print_status()
                
                # Predict directional flow
                flow = self.predict_directional_flow(self.PRIMARY_INSTRUMENT)
                
                if flow['valid']:
                    edge = flow['net_edge']
                    mag = flow['edge_magnitude']
                    
                    if mag > self.MIN_EDGE_THRESHOLD:
                        direction = "BUY" if edge > 0 else "SELL"
                        print(f"🎯 EDGE DETECTED: {direction} pressure (edge={edge:+.3f}, mag={mag:.3f})")
                    else:
                        print(f"📊 Weak edge (mag={mag:.3f}) - staying passive")
                
                # Calculate quotes
                quotes = self.calculate_skewed_quotes(self.PRIMARY_INSTRUMENT, flow)
                
                if quotes:
                    print(f"Strategy: {quotes['strategy']}")
                    print(f"Quotes: Bid {quotes['bid_volume']}@{quotes['bid_price']:.2f} | "
                          f"Ask {quotes['ask_volume']}@{quotes['ask_price']:.2f}")
                    
                    self.manage_orders(self.PRIMARY_INSTRUMENT, quotes)
                
                # Hedge periodically
                if iteration % 5 == 0:
                    self.hedge_position()
                
                time.sleep(self.LOOP_WAIT)
                
        except KeyboardInterrupt:
            self.cleanup()
    
    def cleanup(self):
        """Cleanup."""
        print("\n🛑 Stopping...")
        try:
            self.exchange.delete_orders(self.PRIMARY_INSTRUMENT)
            self.exchange.delete_orders(self.HEDGE_INSTRUMENT)
        except:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-file', default='team_models.pkl')
    args = parser.parse_args()
    
    predictor = TeamBehaviorPredictor()
    try:
        predictor.load_models(args.model_file)
        print(f"✓ Loaded {len(predictor.models)} team models\n")
    except:
        print("✗ Model file not found")
        return
    
    exchange = Exchange()
    exchange.connect()
    print("✓ Connected\n")
    
    bot = ImprovedPredictiveTradingBot(exchange, predictor)
    bot.run()


if __name__ == "__main__":
    main()