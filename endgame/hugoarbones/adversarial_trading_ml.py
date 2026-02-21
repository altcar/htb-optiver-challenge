"""
Adversarial Trading Pattern Detection V3 - With Real Negative Samples

Key improvements:
1. Uses orderbook from BEFORE trades (look-back buffer)
2. Collects REAL negative samples (orderbook states where team didn't trade)
3. Separate BUY/SELL models
4. Balanced training with actual market conditions

Usage:
    python adversarial_trading_ml_v3.py --collect --duration 1800
    python adversarial_trading_ml_v3.py --train
    python adversarial_trading_ml_v3.py --predict
"""

import time
import argparse
import pickle
import json
from datetime import datetime
from collections import defaultdict, deque
from typing import Dict, List, Optional, Tuple, Any
import threading
import random

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, precision_recall_fscore_support
from optibook.synchronous_client import Exchange
from market_moves import OptiBookTradeStream


class OrderBookFeatureExtractor:
    """Extract features from orderbook snapshots."""
    
    @staticmethod
    def extract_features(order_book: Any, instrument_id: str) -> Optional[Dict]:
        """Extract features from orderbook."""
        if not (order_book and order_book.bids and order_book.asks):
            return None
        
        features = {
            'instrument': instrument_id,
            'timestamp': time.time(),
        }
        
        try:
            bids = order_book.bids
            asks = order_book.asks
            
            best_bid_price = bids[0].price
            best_bid_vol = bids[0].volume
            best_ask_price = asks[0].price
            best_ask_vol = asks[0].volume
            
            # Core features
            features['spread'] = best_ask_price - best_bid_price
            features['spread_pct'] = features['spread'] / best_bid_price if best_bid_price > 0 else 0
            features['mid_price'] = (best_bid_price + best_ask_price) / 2
            
            total_tob = best_bid_vol + best_ask_vol
            features['tob_imbalance'] = (best_bid_vol - best_ask_vol) / total_tob if total_tob > 0 else 0
            
            # Depth
            bid_depth_5 = sum(b.volume for b in bids[:5])
            ask_depth_5 = sum(a.volume for a in asks[:5])
            
            features['bid_depth_5'] = bid_depth_5
            features['ask_depth_5'] = ask_depth_5
            total_depth = bid_depth_5 + ask_depth_5
            features['depth_imbalance_5'] = (bid_depth_5 - ask_depth_5) / total_depth if total_depth > 0 else 0
            
            # VWMP
            vwmp_bid = sum(b.price * b.volume for b in bids[:5]) / bid_depth_5 if bid_depth_5 > 0 else best_bid_price
            vwmp_ask = sum(a.price * a.volume for a in asks[:5]) / ask_depth_5 if ask_depth_5 > 0 else best_ask_price
            features['vwmp'] = (vwmp_bid + vwmp_ask) / 2
            
            # Price levels
            for i in range(min(3, len(bids))):
                features[f'bid_price_{i}'] = bids[i].price
                features[f'bid_volume_{i}'] = bids[i].volume
            
            for i in range(min(3, len(asks))):
                features[f'ask_price_{i}'] = asks[i].price
                features[f'ask_volume_{i}'] = asks[i].volume
            
            # Spacing
            if len(bids) >= 2:
                features['bid_price_diff_1'] = bids[0].price - bids[1].price
            if len(asks) >= 2:
                features['ask_price_diff_1'] = asks[1].price - asks[0].price
            
            # Microprice
            microprice_denom = best_bid_vol + best_ask_vol
            if microprice_denom > 0:
                features['microprice'] = (best_bid_price * best_ask_vol + best_ask_price * best_bid_vol) / microprice_denom
            else:
                features['microprice'] = features['mid_price']
            
            features['total_depth'] = bid_depth_5 + ask_depth_5
            features['bid_ask_ratio'] = bid_depth_5 / ask_depth_5 if ask_depth_5 > 0 else 10.0
            
            return features
            
        except Exception as e:
            return None


class ImprovedDataCollector:
    """Collect BOTH positive (trades) and negative (no trades) samples."""
    
    def __init__(self, exchange, instruments: Dict, look_back_seconds: float = 0.5, 
                 look_forward_seconds: float = 5.0, negative_sample_rate: float = 0.1):
        """
        Args:
            exchange: Exchange API
            instruments: Instruments dict
            look_back_seconds: Use orderbook from this many seconds before trade
            look_forward_seconds: Match trades within this window
            negative_sample_rate: Probability of storing a snapshot as potential negative sample
        """
        self.exchange = exchange
        self.instruments = instruments
        self.look_back_seconds = look_back_seconds
        self.look_forward_seconds = look_forward_seconds
        self.negative_sample_rate = negative_sample_rate
        
        # Orderbook history
        self.orderbook_snapshots = defaultdict(lambda: deque(maxlen=5000))
        
        # Training data
        self.positive_samples = []  # Orderbook states that led to trades
        self.negative_candidates = defaultdict(lambda: deque(maxlen=2000))  # Snapshots where team might not have traded
        self.recent_trades = defaultdict(lambda: deque(maxlen=500))  # Track recent trades per team
        
        self.feature_extractor = OrderBookFeatureExtractor()
        self.lock = threading.Lock()
        
        # Stats
        self.snapshot_count = 0
        self.trade_count = 0
        self.positive_count = 0
        self.negative_candidate_count = 0
        
    def capture_orderbook_snapshot(self):
        """Capture orderbook and potentially store as negative sample candidate."""
        for instrument in self.instruments.values():
            try:
                order_book = self.exchange.get_last_price_book(instrument.instrument_id)
                
                if not (order_book and order_book.bids and order_book.asks):
                    continue
                
                features = self.feature_extractor.extract_features(order_book, instrument.instrument_id)
                
                if features:
                    with self.lock:
                        self.orderbook_snapshots[instrument.instrument_id].append(features)
                        self.snapshot_count += 1
                        
                        # Randomly store some snapshots as potential negative samples
                        # We'll later check if a trade happened soon after
                        if random.random() < self.negative_sample_rate:
                            self.negative_candidates[instrument.instrument_id].append({
                                **features,
                                'captured_for_negative': True,
                            })
                            self.negative_candidate_count += 1
                        
            except Exception as e:
                pass
    
    def handle_trade(self, trade: Dict):
        """Process trade and create positive samples."""
        instrument = trade['instrument']
        trade_time = time.time()
        buyer = trade['buyer']
        seller = trade['seller']
        price = trade['price']
        volume = trade['volume']
        
        with self.lock:
            self.trade_count += 1
            
            # Track this trade
            if buyer:
                self.recent_trades[buyer].append({
                    'time': trade_time,
                    'instrument': instrument,
                    'action': 'BUY',
                    'price': price,
                })
            if seller:
                self.recent_trades[seller].append({
                    'time': trade_time,
                    'instrument': instrument,
                    'action': 'SELL',
                    'price': price,
                })
            
            # Find orderbook snapshot from BEFORE the trade
            snapshots = list(self.orderbook_snapshots[instrument])
            
            if not snapshots:
                return
            
            relevant_snapshot = None
            
            for snapshot in reversed(snapshots):
                time_diff = trade_time - snapshot['timestamp']
                
                if self.look_back_seconds <= time_diff <= self.look_forward_seconds:
                    relevant_snapshot = snapshot
                    break
            
            if relevant_snapshot:
                self.positive_count += 1
                mid = relevant_snapshot['mid_price']
                
                # POSITIVE SAMPLE for BUYER
                if buyer:
                    buyer_sample = {
                        **relevant_snapshot,
                        'team': buyer,
                        'action': 'BUY',
                        'label': 1,  # Positive: team DID trade
                        'trade_price': price,
                        'trade_volume': volume,
                        'price_vs_mid': price - mid,
                        'time_diff': time_diff,
                    }
                    self.positive_samples.append(buyer_sample)
                
                # POSITIVE SAMPLE for SELLER
                if seller:
                    seller_sample = {
                        **relevant_snapshot,
                        'team': seller,
                        'action': 'SELL',
                        'label': 1,  # Positive: team DID trade
                        'trade_price': price,
                        'trade_volume': volume,
                        'price_vs_mid': price - mid,
                        'time_diff': time_diff,
                    }
                    self.positive_samples.append(seller_sample)
    
    def generate_negative_samples(self):
        """
        Convert negative candidates to actual negative samples.
        
        A negative candidate becomes a negative sample if:
        - Enough time has passed (no trade happened soon after)
        - We assign it to teams that didn't trade on that instrument at that time
        """
        print("\n📊 Generating negative samples from candidates...")
        
        negative_samples = []
        current_time = time.time()
        
        with self.lock:
            # Get all teams that have traded
            all_teams = set()
            for sample in self.positive_samples:
                all_teams.add(sample['team'])
            
            all_teams = list(all_teams)
            
            for instrument, candidates in self.negative_candidates.items():
                for candidate in candidates:
                    snapshot_time = candidate['timestamp']
                    
                    # Only use candidates that are old enough (no recent trade could match)
                    if current_time - snapshot_time < self.look_forward_seconds + 5:
                        continue
                    
                    # For each team, check if they traded near this time
                    for team in all_teams:
                        # Check if team traded on this instrument around this time
                        team_trades = self.recent_trades.get(team, [])
                        
                        traded_nearby = False
                        for trade in team_trades:
                            if trade['instrument'] == instrument:
                                time_gap = abs(trade['time'] - snapshot_time)
                                if time_gap < self.look_forward_seconds + self.look_back_seconds:
                                    traded_nearby = True
                                    break
                        
                        # If team didn't trade nearby, this is a negative sample for them
                        if not traded_nearby:
                            # Create negative samples for both BUY and SELL
                            for action in ['BUY', 'SELL']:
                                neg_sample = {
                                    **{k: v for k, v in candidate.items() if k != 'captured_for_negative'},
                                    'team': team,
                                    'action': action,
                                    'label': 0,  # Negative: team did NOT trade
                                }
                                negative_samples.append(neg_sample)
        
        print(f"  Generated {len(negative_samples)} negative samples")
        return negative_samples
    
    def finalize_training_data(self):
        """Combine positive and negative samples into balanced dataset."""
        print("\n🔧 Finalizing training data...")
        
        negative_samples = self.generate_negative_samples()
        
        with self.lock:
            # Balance: use same number of negatives as positives per team/action
            team_action_counts = defaultdict(int)
            for sample in self.positive_samples:
                key = (sample['team'], sample['action'])
                team_action_counts[key] += 1
            
            # Sample negatives to match positives
            balanced_negatives = []
            negative_by_team_action = defaultdict(list)
            
            for sample in negative_samples:
                key = (sample['team'], sample['action'])
                negative_by_team_action[key].append(sample)
            
            for key, pos_count in team_action_counts.items():
                available_negatives = negative_by_team_action[key]
                
                # Sample up to 2x positives (for better generalization)
                target_neg_count = min(pos_count * 2, len(available_negatives))
                
                if target_neg_count > 0:
                    sampled = random.sample(available_negatives, target_neg_count)
                    balanced_negatives.extend(sampled)
            
            # Combine
            all_samples = self.positive_samples + balanced_negatives
            random.shuffle(all_samples)
            
            print(f"  Positive samples: {len(self.positive_samples)}")
            print(f"  Negative samples: {len(balanced_negatives)}")
            print(f"  Total samples: {len(all_samples)}")
            
            return all_samples
    
    def print_stats(self):
        """Print collection stats."""
        with self.lock:
            print(f"  Snapshots: {self.snapshot_count} | Trades: {self.trade_count} | "
                  f"Positive: {self.positive_count} | Neg candidates: {self.negative_candidate_count}")
    
    def save_training_data(self, filename: str = 'training_data_v3.json'):
        """Save finalized training data."""
        all_samples = self.finalize_training_data()
        
        with open(filename, 'w') as f:
            json.dump(all_samples, f)
        print(f"\n✓ Saved {len(all_samples)} samples to {filename}")
    
    def load_training_data(self, filename: str = 'training_data_v3.json'):
        """Load training data."""
        with open(filename, 'r') as f:
            data = json.load(f)
        print(f"✓ Loaded {len(data)} samples from {filename}")
        return data


class DirectionalPredictor:
    """Train models to predict BUY vs SELL for each team."""
    
    def __init__(self):
        self.buy_models = {}
        self.sell_models = {}
        self.feature_columns = None
        self.team_stats = defaultdict(dict)
    
    def prepare_data(self, training_data: List[Dict]) -> Tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series]:
        """Prepare features and labels."""
        df = pd.DataFrame(training_data)
        
        if df.empty:
            raise ValueError("No training data")
        
        # Drop non-features
        feature_df = df.drop([
            'team', 'action', 'instrument', 'timestamp', 'label',
            'trade_price', 'trade_volume', 'price_vs_mid', 'time_diff'
        ], axis=1, errors='ignore')
        
        feature_df = feature_df.fillna(0)
        self.feature_columns = feature_df.columns.tolist()
        
        return feature_df, df['team'], df['action'], df['label']
    
    def train_team_models(self, team: str, team_data: pd.DataFrame, team_actions: pd.Series, team_labels: pd.Series):
        """Train BUY and SELL models for a team."""
        print(f"\n  {team}:")
        
        # Separate by action
        buy_mask = team_actions == 'BUY'
        sell_mask = team_actions == 'SELL'
        
        buy_X = team_data[buy_mask]
        buy_y = team_labels[buy_mask]
        
        sell_X = team_data[sell_mask]
        sell_y = team_labels[sell_mask]
        
        # Train BUY model
        if len(buy_X) >= 20 and buy_y.sum() >= 5:  # At least 5 positive samples
            X_train, X_test, y_train, y_test = train_test_split(
                buy_X, buy_y, test_size=0.2, random_state=42, stratify=buy_y
            )
            
            model = xgb.XGBClassifier(
                n_estimators=100,
                max_depth=4,
                learning_rate=0.05,
                objective='binary:logistic',
                random_state=42,
                subsample=0.8,
                colsample_bytree=0.8,
                reg_alpha=0.5,
                reg_lambda=1.0,
                scale_pos_weight=(len(buy_y) - buy_y.sum()) / buy_y.sum(),  # Handle imbalance
            )
            
            model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
            
            y_pred = model.predict(X_test)
            acc = accuracy_score(y_test, y_pred)
            prec, rec, f1, _ = precision_recall_fscore_support(y_test, y_pred, average='binary', zero_division=0)
            
            self.buy_models[team] = model
            print(f"    BUY:  Acc={acc:.3f} Prec={prec:.3f} Rec={rec:.3f} F1={f1:.3f} (n={len(buy_X)}, pos={int(buy_y.sum())})")
        
        # Train SELL model
        if len(sell_X) >= 20 and sell_y.sum() >= 5:
            X_train, X_test, y_train, y_test = train_test_split(
                sell_X, sell_y, test_size=0.2, random_state=42, stratify=sell_y
            )
            
            model = xgb.XGBClassifier(
                n_estimators=100,
                max_depth=4,
                learning_rate=0.05,
                objective='binary:logistic',
                random_state=42,
                subsample=0.8,
                colsample_bytree=0.8,
                reg_alpha=0.5,
                reg_lambda=1.0,
                scale_pos_weight=(len(sell_y) - sell_y.sum()) / sell_y.sum(),
            )
            
            model.fit(X_train, y_train, eval_set=[(X_test, y_test)], verbose=False)
            
            y_pred = model.predict(X_test)
            acc = accuracy_score(y_test, y_pred)
            prec, rec, f1, _ = precision_recall_fscore_support(y_test, y_pred, average='binary', zero_division=0)
            
            self.sell_models[team] = model
            print(f"    SELL: Acc={acc:.3f} Prec={prec:.3f} Rec={rec:.3f} F1={f1:.3f} (n={len(sell_X)}, pos={int(sell_y.sum())})")
        
        self.team_stats[team] = {
            'has_buy_model': team in self.buy_models,
            'has_sell_model': team in self.sell_models,
        }
    
    def train_all_teams(self, training_data: List[Dict], min_samples: int = 20):
        """Train all team models."""
        X, teams, actions, labels = self.prepare_data(training_data)
        
        print(f"\n{'='*80}")
        print("🎓 Training directional models with real negative samples")
        print(f"{'='*80}")
        
        for team in teams.unique():
            team_mask = teams == team
            team_X = X[team_mask]
            team_actions = actions[team_mask]
            team_labels = labels[team_mask]
            
            if len(team_X) < min_samples:
                continue
            
            self.train_team_models(team, team_X, team_actions, team_labels)
        
        n_buy = len(self.buy_models)
        n_sell = len(self.sell_models)
        print(f"\n{'='*80}")
        print(f"✓ Trained {n_buy} BUY models and {n_sell} SELL models")
        print(f"{'='*80}")
    
    def predict_team_direction(self, team: str, orderbook_features: Dict) -> Optional[Dict]:
        """Predict BUY/SELL probabilities for a team."""
        feature_dict = {col: orderbook_features.get(col, 0) for col in self.feature_columns}
        X = pd.DataFrame([feature_dict])
        
        buy_prob = 0.0
        sell_prob = 0.0
        
        if team in self.buy_models:
            buy_proba = self.buy_models[team].predict_proba(X)[0]
            buy_prob = float(buy_proba[1])  # Probability of buying
        
        if team in self.sell_models:
            sell_proba = self.sell_models[team].predict_proba(X)[0]
            sell_prob = float(sell_proba[1])  # Probability of selling
        
        net_direction = buy_prob - sell_prob
        
        return {
            'team': team,
            'buy_probability': buy_prob,
            'sell_probability': sell_prob,
            'net_direction': net_direction,
        }
    
    def predict_all_teams(self, orderbook_features: Dict) -> List[Dict]:
        """Predict for all teams."""
        predictions = []
        all_teams = set(list(self.buy_models.keys()) + list(self.sell_models.keys()))
        
        for team in all_teams:
            pred = self.predict_team_direction(team, orderbook_features)
            if pred:
                predictions.append(pred)
        
        predictions.sort(key=lambda x: abs(x['net_direction']), reverse=True)
        return predictions
    
    def save_models(self, filename: str = 'models_v3.pkl'):
        """Save models."""
        data = {
            'buy_models': self.buy_models,
            'sell_models': self.sell_models,
            'feature_columns': self.feature_columns,
            'team_stats': dict(self.team_stats),
        }
        with open(filename, 'wb') as f:
            pickle.dump(data, f)
        print(f"\n✓ Saved to {filename}")
    
    def load_models(self, filename: str = 'models_v3.pkl'):
        """Load models."""
        with open(filename, 'rb') as f:
            data = pickle.load(f)
        
        self.buy_models = data['buy_models']
        self.sell_models = data['sell_models']
        self.feature_columns = data['feature_columns']
        self.team_stats = defaultdict(dict, data['team_stats'])
        print(f"✓ Loaded from {filename}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--collect', action='store_true')
    parser.add_argument('--train', action='store_true')
    parser.add_argument('--predict', action='store_true')
    parser.add_argument('--duration', type=int, default=900)
    parser.add_argument('--data-file', default='training_data_v3.json')
    parser.add_argument('--model-file', default='models_v3.pkl')
    parser.add_argument('--look-back', type=float, default=2.0)
    parser.add_argument('--negative-rate', type=float, default=0.1)
    
    args = parser.parse_args()
    
    if args.collect:
        mode = 'collect'
    elif args.train:
        mode = 'train'
    elif args.predict:
        mode = 'predict'
    else:
        parser.error("Specify --collect, --train, or --predict")
    
    print(f"\n{'='*80}")
    print("🤖 ADVERSARIAL TRADING ML V3 - With Real Negative Samples")
    print(f"{'='*80}")
    
    try:
        exchange = Exchange()
        exchange.connect()
        instruments = exchange.get_instruments()
        print(f"✓ Connected | Instruments: {len(instruments)}")
    except Exception as e:
        print(f"✗ Error: {e}")
        return
    
    if mode == 'collect':
        print(f"\n📊 COLLECTING (look-back={args.look_back}s, neg-rate={args.negative_rate})")
        print(f"{'='*80}\n")
        
        collector = ImprovedDataCollector(
            exchange, instruments, 
            look_back_seconds=args.look_back,
            negative_sample_rate=args.negative_rate
        )
        
        stream = OptiBookTradeStream("htb.optibook.net", "hugoarbones")
        stream.on_trade = collector.handle_trade
        stream.on_connected = lambda: print("✓ Trade stream connected\n")
        stream.start(blocking=False)
        
        start = time.time()
        last_stats = start
        
        try:
            print("Collecting... (Ctrl+C to stop)\n")
            
            while time.time() - start < args.duration:
                collector.capture_orderbook_snapshot()
                time.sleep(0.5)
                
                if time.time() - last_stats >= 20:
                    collector.print_stats()
                    last_stats = time.time()
        
        except KeyboardInterrupt:
            print("\n\nStopping...")
        
        finally:
            stream.stop()
            collector.print_stats()
            collector.save_training_data(args.data_file)
    
    elif mode == 'train':
        print(f"\n🎓 TRAINING")
        print(f"{'='*80}\n")
        
        predictor = DirectionalPredictor()
        collector = ImprovedDataCollector(exchange, instruments)
        
        try:
            data = collector.load_training_data(args.data_file)
        except FileNotFoundError:
            print(f"✗ File not found: {args.data_file}")
            return
        
        if len(data) == 0:
            print("✗ No data")
            return
        
        predictor.train_all_teams(data, min_samples=20)
        predictor.save_models(args.model_file)
    
    elif mode == 'predict':
        print(f"\n🔮 LIVE PREDICTION")
        print(f"{'='*80}\n")
        
        predictor = DirectionalPredictor()
        try:
            predictor.load_models(args.model_file)
        except:
            print(f"✗ Model file not found")
            return
        
        extractor = OrderBookFeatureExtractor()
        
        try:
            while True:
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}]")
                
                for inst in list(instruments.values())[:2]:
                    try:
                        ob = exchange.get_last_price_book(inst.instrument_id)
                        if not (ob and ob.bids and ob.asks):
                            continue
                        
                        feat = extractor.extract_features(ob, inst.instrument_id)
                        if not feat:
                            continue
                        
                        preds = predictor.predict_all_teams(feat)
                        
                        print(f"\n{inst.instrument_id} (mid={feat['mid_price']:.2f}):")
                        for p in preds[:5]:
                            if abs(p['net_direction']) > 0.25:
                                dir_str = "📈 BUY" if p['net_direction'] > 0 else "📉 SELL"
                                print(f"  {p['team']:15s}: {dir_str} (buy={p['buy_probability']:.2f}, sell={p['sell_probability']:.2f})")
                    except:
                        pass
                
                time.sleep(5)
        
        except KeyboardInterrupt:
            print("\n\nStopping...")


if __name__ == "__main__":
    main()