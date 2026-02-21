import asyncio
import datetime as dt
import logging
import time
import sqlite3
import json
from math import floor, ceil
from collections import deque
from threading import Thread, Lock

import numpy as np
from optibook.synchronous_client import Exchange

logging.getLogger("client").setLevel("ERROR")
log = logging.getLogger("bot")
log.setLevel(logging.INFO)

# ------------------------------------------------
# CONFIGURATION
# ------------------------------------------------
POSITION_LIMIT = 100
HISTORY_WINDOW = 50
REBALANCE_INTERVAL = 5

# News-driven trading parameters
NEWS_REACTION_WINDOW = 30  # seconds after news to look for opportunities
NEWS_POSITION_MULTIPLIER = 0.6  # Use 60% of detected volume surge
MAX_POSITION_SIZE = 1000  # Maximum position size per trade
MIN_PRICE_CHANGE = 0.03  # Minimum price change to trigger trade
MIN_VOLUME_SURGE = 50  # Minimum volume surge to consider
NEWS_DB_PATH = "market_data.db"
TIMESTAMP_CORRECTION = 2232  # Correction for news timestamp offset

# Adaptive reconnect backoff
RECONNECT_LOWER = 0.0
RECONNECT_UPPER = 2.0
RECONNECT_SAMPLES = 3

# ------------------------------------------------
# STATE
# ------------------------------------------------
history = {sym: deque(maxlen=HISTORY_WINDOW) for sym in ["NVDA", "PFE", "CSCO", "SAN", "ING"]}
current_reconnect_delay = (RECONNECT_LOWER + RECONNECT_UPPER) / 2
news_monitor_lock = Lock()
recent_news = deque(maxlen=100)  # Keep track of recent news events
active_positions = {}  # Track positions opened due to news


class NewsMonitor:
    """Monitor news events and detect high-impact opportunities"""
    
    def __init__(self, db_path):
        self.db_path = db_path
        self.last_news_timestamp = 0
        
    def get_recent_news_events(self):
        """Get news events from the last few minutes"""
        current_time = time.time()
        lookback_time = current_time - 180  # Look back 3 minutes
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Apply timestamp correction and get recent news
        corrected_lookback = lookback_time - TIMESTAMP_CORRECTION
        
        cursor.execute("""
            SELECT timestamp, headline, instrument 
            FROM news 
            WHERE timestamp > ?
            ORDER BY timestamp DESC
            LIMIT 50
        """, (corrected_lookback,))
        
        news_events = cursor.fetchall()
        conn.close()
        
        # Apply timestamp correction to results
        corrected_events = []
        for timestamp, headline, instrument in news_events:
            corrected_timestamp = float(timestamp) + TIMESTAMP_CORRECTION
            corrected_events.append((corrected_timestamp, headline, instrument))
        
        return corrected_events
    
    def extract_instruments_from_headline(self, headline):
        """Extract instrument mentions from news headline"""
        instruments = []
        headline_upper = headline.upper()
        
        # Check for direct mentions
        if 'NVIDIA' in headline_upper or 'NVDA' in headline_upper:
            instruments.append('NVDA')
        if 'CISCO' in headline_upper or 'CSCO' in headline_upper:
            instruments.append('CSCO')
        if 'ING' in headline_upper:
            instruments.append('ING')
        if 'SANTANDER' in headline_upper or 'SAN' in headline_upper:
            instruments.append('SAN')
        if 'PFIZER' in headline_upper or 'PFE' in headline_upper:
            instruments.append('PFE')
            
        return instruments if instruments else ['NVDA', 'CSCO', 'ING', 'SAN', 'PFE']  # Default to all if unclear
    
    def assess_news_impact(self, headline):
        """Assess the potential impact of a news headline"""
        headline_lower = headline.lower()
        
        # High impact keywords
        high_impact_words = [
            'breakthrough', 'major', 'significant', 'record', 'surge', 'plunge',
            'earnings', 'revenue', 'profit', 'loss', 'merger', 'acquisition',
            'lawsuit', 'settlement', 'approval', 'fda', 'partnership', 'deal',
            'crisis', 'scandal', 'investigation', 'fine', 'penalty'
        ]
        
        # Medium impact keywords
        medium_impact_words = [
            'increase', 'decrease', 'growth', 'decline', 'expansion', 'contract',
            'launch', 'release', 'update', 'upgrade', 'downgrade', 'forecast'
        ]
        
        impact_score = 0
        for word in high_impact_words:
            if word in headline_lower:
                impact_score += 3
        
        for word in medium_impact_words:
            if word in headline_lower:
                impact_score += 1
                
        # Normalize to 0-1 scale
        return min(impact_score / 10.0, 1.0)


def test_reconnect_delay(exchange, delay):
    """Try connecting several times and measure failure/success."""
    successes = 0
    for _ in range(RECONNECT_SAMPLES):
        if not exchange.is_connected():
            try:
                time.sleep(delay)
                exchange.connect()
                if exchange.is_connected():
                    successes += 1
            except:
                pass
        else:
            successes += 1
    return successes / RECONNECT_SAMPLES


def adapt_reconnect_delay(exchange):
    global RECONNECT_LOWER, RECONNECT_UPPER, current_reconnect_delay

    mid = (RECONNECT_LOWER + RECONNECT_UPPER) / 2
    performance = test_reconnect_delay(exchange, mid)

    if performance < 0.5:
        # too unreliable, increase delay
        RECONNECT_LOWER = mid
    else:
        # reliable enough, decrease
        RECONNECT_UPPER = mid

    current_reconnect_delay = (RECONNECT_LOWER + RECONNECT_UPPER) / 2


def ensure_connected(exchange):
    if exchange.is_connected():
        return
    adapt_reconnect_delay(exchange)
    time.sleep(current_reconnect_delay)
    while not exchange.is_connected():
        try:
            exchange.connect()
        except:
            time.sleep(current_reconnect_delay)


def compute_mid_price(book):
    if book and book.bids and book.asks:
        return (book.bids[0].price + book.asks[0].price) / 2
    return None


def get_recent_trade_data(instrument, lookback_seconds=60):
    """Get recent trade data for price movement analysis"""
    current_time = time.time()
    lookback_time = current_time - lookback_seconds
    
    conn = sqlite3.connect(NEWS_DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT timestamp, price, volume 
        FROM trades 
        WHERE instrument = ? AND timestamp > ?
        ORDER BY timestamp
    """, (instrument, lookback_time))
    
    trades = cursor.fetchall()
    conn.close()
    
    return [(float(t[0]), float(t[1]), float(t[2])) for t in trades]


def analyze_price_movement(trades_before, trades_after):
    """Analyze price movement between before and after periods"""
    if not trades_before or not trades_after:
        return 0, 0
    
    # Calculate average prices
    before_prices = [t[1] for t in trades_before]
    after_prices = [t[1] for t in trades_after]
    
    avg_before = np.mean(before_prices)
    avg_after = np.mean(after_prices)
    
    price_change = abs(avg_after - avg_before)
    
    # Calculate volume surge
    before_volume = sum(t[2] for t in trades_before)
    after_volume = sum(t[2] for t in trades_after)
    
    return price_change, after_volume


def execute_news_driven_trade(exchange, instrument, news_event, price_change, volume_surge):
    """Execute a news-driven trade based on detected opportunity"""
    
    current_time, headline, _ = news_event
    
    # Calculate position size based on volume surge
    position_size = min(volume_surge * NEWS_POSITION_MULTIPLIER, MAX_POSITION_SIZE)
    position_size = max(position_size, 10)  # Minimum 10 shares
    position_size = int(position_size)
    
    try:
        # Get current market data
        book = exchange.get_last_price_book(instrument)
        if not book or not book.bids or not book.asks:
            log.warning(f"No book data for {instrument}")
            return False
        
        # Check current position
        positions = exchange.get_positions()
        current_position = positions.get(instrument, 0)
        
        # Determine trade direction based on headline sentiment
        headline_lower = headline.lower()
        is_positive_news = any(word in headline_lower for word in [
            'growth', 'profit', 'revenue', 'breakthrough', 'partnership', 
            'approval', 'expansion', 'success', 'record', 'surge'
        ])
        
        is_negative_news = any(word in headline_lower for word in [
            'loss', 'decline', 'crisis', 'lawsuit', 'fine', 'investigation',
            'scandal', 'plunge', 'drop', 'fall'
        ])
        
        # Execute trade based on sentiment and position limits
        if is_positive_news and current_position + position_size <= POSITION_LIMIT:
            # Buy on positive news
            price = book.asks[0].price
            try:
                result = exchange.insert_order(
                    instrument_id=instrument,
                    price=price,
                    volume=position_size,
                    side='bid',
                    order_type='ioc'  # Immediate or cancel
                )
                
                if result:
                    log.info(f"📈 NEWS BUY: {instrument} {position_size}@{price:.3f} | News: {headline[:50]}...")
                    
                    # Track position
                    active_positions[f"{instrument}_{current_time}"] = {
                        'instrument': instrument,
                        'side': 'buy',
                        'volume': position_size,
                        'entry_price': price,
                        'entry_time': current_time,
                        'news': headline[:50]
                    }
                    return True
                    
            except Exception as e:
                log.error(f"Error executing buy order: {e}")
                
        elif is_negative_news and current_position - position_size >= -POSITION_LIMIT:
            # Sell on negative news
            price = book.bids[0].price
            try:
                result = exchange.insert_order(
                    instrument_id=instrument,
                    price=price,
                    volume=position_size,
                    side='ask',
                    order_type='ioc'
                )
                
                if result:
                    log.info(f"📉 NEWS SELL: {instrument} {position_size}@{price:.3f} | News: {headline[:50]}...")
                    
                    # Track position
                    active_positions[f"{instrument}_{current_time}"] = {
                        'instrument': instrument,
                        'side': 'sell',
                        'volume': position_size,
                        'entry_price': price,
                        'entry_time': current_time,
                        'news': headline[:50]
                    }
                    return True
                    
            except Exception as e:
                log.error(f"Error executing sell order: {e}")
    
    except Exception as e:
        log.error(f"Error in news-driven trade execution: {e}")
    
    return False


def check_news_opportunities(exchange, news_monitor):
    """Check for news-driven trading opportunities"""
    current_time = time.time()
    
    # Get recent news events
    news_events = news_monitor.get_recent_news_events()
    
    opportunities_found = 0
    
    for news_timestamp, headline, news_instrument in news_events:
        # Skip if news is too old or too new
        time_since_news = current_time - news_timestamp
        if time_since_news < 3 or time_since_news > NEWS_REACTION_WINDOW:
            continue
        
        # Skip if we've already processed this news
        news_key = f"{news_timestamp}_{headline[:30]}"
        if news_key in [str(item) for item in recent_news]:
            continue
            
        # Add to processed news
        with news_monitor_lock:
            recent_news.append(news_key)
        
        # Extract relevant instruments
        instruments = news_monitor.extract_instruments_from_headline(headline)
        impact_score = news_monitor.assess_news_impact(headline)
        
        # Only process high-impact news
        if impact_score < 0.3:
            continue
        
        for instrument in instruments:
            # Get trade data before and after news
            trades_before = get_recent_trade_data(instrument, lookback_seconds=60)
            trades_before = [t for t in trades_before if t[0] < news_timestamp]
            
            trades_after = get_recent_trade_data(instrument, lookback_seconds=60)
            trades_after = [t for t in trades_after if t[0] > news_timestamp and t[0] <= news_timestamp + 30]
            
            if not trades_before or not trades_after:
                continue
            
            # Analyze price movement and volume
            price_change, volume_surge = analyze_price_movement(trades_before, trades_after)
            
            # Check if this meets our criteria for high-impact trading
            if price_change >= MIN_PRICE_CHANGE and volume_surge >= MIN_VOLUME_SURGE:
                log.info(f"🚨 HIGH-IMPACT NEWS OPPORTUNITY: {instrument}")
                log.info(f"   News: {headline[:60]}...")
                log.info(f"   Price change: ${price_change:.3f}")
                log.info(f"   Volume surge: {volume_surge:.0f}")
                log.info(f"   Impact score: {impact_score:.2f}")
                
                # Execute the trade
                success = execute_news_driven_trade(
                    exchange, instrument, (news_timestamp, headline, news_instrument),
                    price_change, volume_surge
                )
                
                if success:
                    opportunities_found += 1
    
    return opportunities_found


def manage_active_positions(exchange):
    """Manage and close active news-driven positions"""
    current_time = time.time()
    positions_to_close = []
    
    for position_key, position_info in active_positions.items():
        # Close positions after 5 minutes or if profit target hit
        time_in_position = current_time - position_info['entry_time']
        
        if time_in_position > 300:  # 5 minutes
            positions_to_close.append(position_key)
        else:
            # Check if we should close based on profit/loss
            try:
                book = exchange.get_last_price_book(position_info['instrument'])
                if book and book.bids and book.asks:
                    current_price = (book.bids[0].price + book.asks[0].price) / 2
                    entry_price = position_info['entry_price']
                    
                    if position_info['side'] == 'buy':
                        pnl = (current_price - entry_price) * position_info['volume']
                    else:
                        pnl = (entry_price - current_price) * position_info['volume']
                    
                    # Close if profit > $20 or loss > $10
                    if pnl > 20 or pnl < -10:
                        positions_to_close.append(position_key)
                        
            except Exception as e:
                log.error(f"Error checking position {position_key}: {e}")
    
    # Close positions
    for position_key in positions_to_close:
        position_info = active_positions[position_key]
        try:
            book = exchange.get_last_price_book(position_info['instrument'])
            if book and book.bids and book.asks:
                
                if position_info['side'] == 'buy':
                    # Close buy position with sell
                    price = book.bids[0].price
                    result = exchange.insert_order(
                        instrument_id=position_info['instrument'],
                        price=price,
                        volume=position_info['volume'],
                        side='ask',
                        order_type='ioc'
                    )
                else:
                    # Close sell position with buy
                    price = book.asks[0].price
                    result = exchange.insert_order(
                        instrument_id=position_info['instrument'],
                        price=price,
                        volume=position_info['volume'],
                        side='bid',
                        order_type='ioc'
                    )
                
                if result:
                    entry_price = position_info['entry_price']
                    if position_info['side'] == 'buy':
                        pnl = (price - entry_price) * position_info['volume']
                    else:
                        pnl = (entry_price - price) * position_info['volume']
                    
                    log.info(f"🔄 CLOSED: {position_info['instrument']} | P&L: ${pnl:.2f}")
                    del active_positions[position_key]
                    
        except Exception as e:
            log.error(f"Error closing position {position_key}: {e}")


def main():
    exchange = Exchange()
    ensure_connected(exchange)
    
    # Initialize news monitor
    news_monitor = NewsMonitor(NEWS_DB_PATH)
    
    log.info("🚀 News-Driven Trading Controller Started")
    log.info(f"📊 Parameters: Max position: {MAX_POSITION_SIZE}, Min price change: ${MIN_PRICE_CHANGE}")
    
    cycle_count = 0
    total_opportunities = 0
    
    while True:
        ensure_connected(exchange)
        cycle_count += 1
        
        # Update price history
        for sym in history.keys():
            book = exchange.get_last_price_book(sym)
            mid = compute_mid_price(book)
            if mid is not None:
                history[sym].append(mid)
        
        # Check for news opportunities every cycle
        try:
            opportunities = check_news_opportunities(exchange, news_monitor)
            total_opportunities += opportunities
            
            if opportunities > 0:
                log.info(f"✅ Found {opportunities} news opportunities this cycle")
        
        except Exception as e:
            log.error(f"Error checking news opportunities: {e}")
        
        # Manage existing positions
        try:
            manage_active_positions(exchange)
        except Exception as e:
            log.error(f"Error managing positions: {e}")
        
        # Status update every 20 cycles (100 seconds)
        if cycle_count % 20 == 0:
            log.info(f"📈 Status: {total_opportunities} total opportunities found, {len(active_positions)} active positions")
            
            # Show current positions
            if active_positions:
                log.info("🎯 Active positions:")
                for key, pos in active_positions.items():
                    log.info(f"   {pos['instrument']}: {pos['side']} {pos['volume']} shares")

        time.sleep(REBALANCE_INTERVAL)


if __name__ == "__main__":
    main()