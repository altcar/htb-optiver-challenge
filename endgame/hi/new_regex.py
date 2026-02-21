import datetime as dt
import time
import logging
import re
from collections import defaultdict, deque
from math import floor, ceil

from optibook.synchronous_client import Exchange
from optibook.common_types import SocialMediaFeed

logging.getLogger("client").setLevel("ERROR")

# Stock-specific keywords for relevance check
STOCK_KEYWORDS = {
    'NVDA': ['nvidia', 'nvda', 'gpu', 'ai chip'],
    'ING': ['ing', 'dutch bank'],
    'SAN': ['santander', 'san'],
    'PFE': ['pfizer', 'pfe', 'vaccine', 'pharma'],
    'CSCO': ['cisco', 'csco', 'networking']
}

# Sentiment lexicon tuned on training.csv patterns
POSITIVE_WORDS = [
    'strong', 'boost', 'upgrade', 'growth', 'positive', 'gain', 'bullish', 'buy', 'outperform',
    'expansion', 'robust', 'improve', 'rise', 'win', 'success', 'satisfied', 'celebrated',
    'breakthrough', 'announces', 'launches', 'wins', 'upgraded', 'better', 'higher'
]
NEGATIVE_WORDS = [
    'weak', 'decline', 'downgrade', 'loss', 'negative', 'drop', 'bearish', 'sell', 'underperform',
    'contraction', 'worsen', 'fall', 'fail', 'lawsuit', 'backlash', 'delay', 'miss', 'cut',
    'pressure', 'slack', 'headwinds', 'faces', 'warns', 'lowers', 'downgraded', 'worse', 'lower'
]

def simple_sentiment(post, stock_id):
    """Rule-based sentiment scoring: Keyword count with normalization."""
    post_lower = post.lower()
    keywords = STOCK_KEYWORDS.get(stock_id, [])
    if not any(re.search(rf'\b{re.escape(kw)}\b', post_lower) for kw in keywords):
        return 0.0  # Neutral if not relevant to stock
    
    # Extract words
    words = re.findall(r'\b\w+\b', post_lower)
    pos_count = sum(1 for w in words if w in POSITIVE_WORDS)
    neg_count = sum(1 for w in words if w in NEGATIVE_WORDS)
    
    # Net score normalized by post length
    net_score = (pos_count - neg_count) / max(len(words), 1)
    
    # Threshold for classification
    if net_score > 0.1:
        return 1.0  # Strong positive
    elif net_score < -0.1:
        return -1.0  # Strong negative
    return 0.0  # Neutral

def print_positions_and_pnl(exchange):
    positions = exchange.get_positions()
    pnl = exchange.get_pnl()

    print("Positions:")
    for instrument_id in positions:
        print(f"  {instrument_id:10s}: {positions[instrument_id]:4.0f}")

    print(f"\nPnL: {pnl:.2f}")

def round_down_to_tick(price, tick_size):
    """Rounds a price down to the nearest tick."""
    return floor(price / tick_size) * tick_size

def round_up_to_tick(price, tick_size):
    """Rounds a price up to the nearest tick."""
    return ceil(price / tick_size) * tick_size

def insert_quotes(exchange, instrument, bid_price, ask_price, bid_volume, ask_volume):
    """Inserts limit orders for bid and ask."""
    if bid_volume > 0:
        exchange.insert_order(
            instrument_id=instrument.instrument_id,
            price=bid_price,
            volume=bid_volume,
            side="bid",
            order_type="limit",
        )
        time.sleep(0.05)

    if ask_volume > 0:
        exchange.insert_order(
            instrument_id=instrument.instrument_id,
            price=ask_price,
            volume=ask_volume,
            side="ask",
            order_type="limit",
        )
        time.sleep(0.05)

def get_aggregated_sentiment(history, stock_id, window=5):
    """Aggregate sentiment scores over recent feeds."""
    recent_feeds = list(history)[-window:]
    scores = []
    for feed in recent_feeds:
        score = simple_sentiment(feed['post'], stock_id)
        if abs(score) > 0:  # Only count classified feeds
            scores.append(score)
    
    if not scores:
        return 0.0
    return sum(scores) / len(scores)  # Average: >0.5 positive, <-0.5 negative

# Main exchange setup
exchange = Exchange()
exchange.connect()

INSTRUMENTS = exchange.get_instruments()

# Strategy parameters
QUOTED_VOLUME = 10
FIXED_MINIMUM_CREDIT = 0.15
PRICE_RETREAT_PER_LOT = 0.005
POSITION_LIMIT = 100

# Sentiment tracking
SENTIMENT_HISTORY = defaultdict(deque)  # Per-stock recent feeds (max 10)
sentiment_states = {inst: {'last_update': 0, 'wait_until': 0} for inst in INSTRUMENTS}
STABILIZATION_WAIT = 18  # Seconds to wait after negative signal

print("Sentiment-Aware Quoter Started. Using rule-based keyword sentiment analysis.")

while True:
    print(f"\n-----------------------------------------------------------------")
    print(f"TRADE LOOP ITERATION ENTERED AT {str(dt.datetime.now()):18s} UTC.")
    print(f"-----------------------------------------------------------------")

    # Display positions and PnL
    print_positions_and_pnl(exchange)

    # Poll new social media feeds
    new_feeds = exchange.poll_new_social_media_feeds()
    now = time.time()
    for feed in new_feeds:
        stored_feed = {'timestamp': feed.timestamp, 'post': feed.post}
        # Append to all histories (filter per-stock in classifier)
        for stock_id in INSTRUMENTS:
            SENTIMENT_HISTORY[stock_id].append(stored_feed)
            if len(SENTIMENT_HISTORY[stock_id]) > 10:
                SENTIMENT_HISTORY[stock_id].popleft()
        print(f"New Feed: {feed.timestamp}: {feed.post[:100]}...")

    print(f"          (ourbid) mktbid :: mktask (ourask)")

    for instrument in INSTRUMENTS.values():
        stock_id = instrument.instrument_id

        # Cancel all existing orders
        exchange.delete_orders(stock_id)

        # Get order book
        order_book = exchange.get_last_price_book(stock_id)
        if not (order_book and order_book.bids and order_book.asks):
            print(f"{stock_id:>6s} --     INCOMPLETE ORDER BOOK")
            continue

        # Current position
        position = exchange.get_positions()[stock_id]

        # Market mid-price
        best_bid_price = order_book.bids[0].price
        best_ask_price = order_book.asks[0].price
        mid_price = (best_bid_price + best_ask_price) / 2.0

        # Theoretical price with position retreat
        theoretical_price = mid_price - PRICE_RETREAT_PER_LOT * position

        # Aggregate sentiment
        sentiment_score = get_aggregated_sentiment(SENTIMENT_HISTORY[stock_id], stock_id)
        state = sentiment_states[stock_id]

        # Sentiment-driven adjustments
        credit = FIXED_MINIMUM_CREDIT
        extra_retreat = 0.0
        vol_multiplier = 1.0
        should_quote = True

        if sentiment_score < -0.5:  # Strong negative: Cancel and wait
            print(f"{stock_id}: Strong negative sentiment ({sentiment_score:.2f}) - Canceling orders & waiting")
            state['wait_until'] = now + STABILIZATION_WAIT
            should_quote = False
        elif now < state['wait_until']:
            wait_time = dt.datetime.fromtimestamp(state['wait_until'])
            print(f"{stock_id}: Stabilizing... (wait until {wait_time})")
            should_quote = False
        elif sentiment_score > 0.5:  # Strong positive: Tighten spreads, increase volume
            credit *= 0.8  # 20% tighter
            vol_multiplier = 1.5  # 50% more volume
            print(f"{stock_id}: Strong positive sentiment ({sentiment_score:.2f}) - Tightening spreads")
        else:
            print(f"{stock_id}: Neutral sentiment ({sentiment_score:.2f})")

        state['last_update'] = now

        if not should_quote:
            continue

        # Apply extra retreat if mildly negative
        if -0.5 < sentiment_score < -0.1:
            extra_retreat = 0.02  # Conservative retreat

        theoretical_price -= extra_retreat * abs(position) / POSITION_LIMIT  # Scale by position size

        # Final bid/ask prices
        bid_price = round_down_to_tick(
            theoretical_price - credit, instrument.tick_size
        )
        ask_price = round_up_to_tick(
            theoretical_price + credit, instrument.tick_size
        )

        # Volumes respecting position limits
        max_volume_to_buy = POSITION_LIMIT - position
        max_volume_to_sell = POSITION_LIMIT + position

        bid_volume = min(QUOTED_VOLUME * vol_multiplier, max_volume_to_buy)
        ask_volume = min(QUOTED_VOLUME * vol_multiplier, max_volume_to_sell)

        # Display quoting info
        print(
            f"{stock_id:>6s} -- ({bid_price:>6.2f}) {best_bid_price:>6.2f} :: {best_ask_price:>6.2f} ({ask_price:>6.2f}) | Sent: {sentiment_score:.2f}"
        )

        # Insert quotes
        insert_quotes(
            exchange, instrument, bid_price, ask_price, bid_volume, ask_volume
        )

        # Added sleep to respect rate limits and avoid max transactions exceeded
        time.sleep(0.3)

    print(f"\nWaiting for 2 seconds.")
