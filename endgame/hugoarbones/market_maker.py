import asyncio
import datetime as dt
import logging
from math import floor, ceil

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from optibook.synchronous_client import Exchange

logging.getLogger("client").setLevel("ERROR")

# ----------------------------
# SENTIMENT MODEL SETUP
# ----------------------------
tokenizer = AutoTokenizer.from_pretrained("nlptown/bert-base-multilingual-uncased-sentiment")
model = AutoModelForSequenceClassification.from_pretrained(
    "nlptown/bert-base-multilingual-uncased-sentiment"
)
model.eval()

def batch_sentiment_score(texts):
    if not texts:
        return []
    inputs = tokenizer(texts, return_tensors="pt", truncation=True, padding=True)
    with torch.no_grad():
        outputs = model(**inputs)
        probs = torch.softmax(outputs.logits, dim=1).numpy()
    # Map to -100..100
    scores = probs @ [-100, -50, 0, 50, 100]
    return scores

# ----------------------------
# HELPER FUNCTIONS
# ----------------------------
def round_down_to_tick(price, tick_size):
    return floor(price / tick_size) * tick_size

def round_up_to_tick(price, tick_size):
    return ceil(price / tick_size) * tick_size

async def safe_exchange_call(func, *args, **kwargs):
    global exchange
    try:
        return func(*args, **kwargs)
    except AssertionError as e:
        print(f"Exchange disconnected: {e}")
        try:
            exchange = Exchange()
            exchange.connect()
            return func(*args, **kwargs)
        except Exception as e2:
            print(f"Reconnect failed: {e2}")
            return None
    except Exception as e:
        print(f"Exchange call failed: {e}")
        return None

async def insert_order_safe(exchange, instrument_id, price, volume, side):
    if volume <= 0:
        return
    await safe_exchange_call(
        exchange.insert_order,
        instrument_id=instrument_id,
        price=price,
        volume=volume,
        side=side,
        order_type="limit"
    )
    await asyncio.sleep(0.05)

# ----------------------------
# SIGNALS
# ----------------------------
def get_sentiment_scores(news_feed, instruments):
    scores = {inst: [] for inst in instruments}
    texts, mapping = [], []
    for item in news_feed:
        for inst_id in getattr(item, "instruments", []):
            if inst_id in instruments:
                texts.append(getattr(item, "text", ""))
                mapping.append(inst_id)
    if texts:
        sentiments = batch_sentiment_score(texts)
        for inst_id, s in zip(mapping, sentiments):
            scores[inst_id].append(s)
    return {k: (sum(v)/len(v) if v else 0) for k, v in scores.items()}

def get_order_flow_imbalance(order_book):
    bid_vol = sum([l.volume for l in order_book.bids])
    ask_vol = sum([l.volume for l in order_book.asks])
    return (bid_vol - ask_vol) * 0.00015

def detect_momentum(history, horizon=3):
    if len(history) < horizon:
        return 0
    mids = [(bids[0].price + asks[0].price)/2 for bids, asks in history[-horizon:]]
    return (mids[-1] - mids[0]) * 0.5

def estimate_volatility(history, horizon=5):
    if len(history) < horizon:
        return 0.01
    mids = [(bids[0].price + asks[0].price)/2 for bids, asks in history[-horizon:]]
    returns = [mids[i+1]-mids[i] for i in range(len(mids)-1)]
    return max(0.01, sum([abs(r) for r in returns])/len(returns))

def risk_adjustment(position, volatility, position_limit, performance_factor):
    return max(
        0.1,
        (1 - abs(position)/position_limit) * performance_factor * (1/(1+volatility*10))
    )

def update_signal_weights(signal_weights, signal_values, mid_change, learning_rate=0.1):
    for s in signal_weights:
        signal = signal_values[s]
        if mid_change*signal > 0:
            signal_weights[s] += learning_rate * (1 - signal_weights[s])
        else:
            signal_weights[s] -= learning_rate * signal_weights[s]
    return signal_weights

# ----------------------------
# CONFIG
# ----------------------------
BASE_QUOTED_VOLUME = 10
BASE_SPREAD = 0.15
POSITION_LIMIT = 100
PRICE_RETREAT_SCALE = 0.01
VOLATILITY_SCALE = 0.5
SIGNAL_LEARNING_RATE = 0.1
SIGNAL_THRESHOLD = 80
MAIN_LOOP_DELAY = 2  # throttled to avoid RPS issues

# ----------------------------
# EXCHANGE INIT
# ----------------------------
exchange = Exchange()
exchange.connect()
INSTRUMENTS = exchange.get_instruments()
instrument_ids = list(INSTRUMENTS.keys())

order_book_history = {inst_id: [] for inst_id in instrument_ids}
signal_weights = {inst_id: {'sentiment':0, 'momentum':0.33, 'order_flow':0} for inst_id in instrument_ids}
performance_factor = 1.0

# ----------------------------
# MAIN ASYNC LOOP
# ----------------------------
async def market_maker_loop():
    global performance_factor
    while True:
        start_time = dt.datetime.now()
        print(f"\n{'-'*65}\nTRADE LOOP ITERATION AT {start_time} UTC\n{'-'*65}")

        # Fetch positions and PnL
        positions = await safe_exchange_call(exchange.get_positions) or {}
        pnl = await safe_exchange_call(exchange.get_pnl) or 0
        positions = {inst: max(-POSITION_LIMIT, min(POSITION_LIMIT, pos)) for inst, pos in positions.items()}

        print("Positions:")
        for inst, pos in positions.items():
            print(f"  {inst:10s}: {pos:4.0f}")
        print(f"\nPnL: {pnl:.2f}\n")

        # Poll news asynchronously less frequently
        news_feed = await safe_exchange_call(exchange.poll_new_social_media_feeds) or []
        sentiment_scores = get_sentiment_scores(news_feed, instrument_ids)

        tasks = []
        for instrument_id in instrument_ids:
            # Only fetch outstanding orders when necessary
            existing_orders = await safe_exchange_call(exchange.get_outstanding_orders, instrument_id) or []

            order_book = await safe_exchange_call(exchange.get_last_price_book, instrument_id)
            if not (order_book and order_book.bids and order_book.asks):
                continue

            history = order_book_history[instrument_id]
            history.append((order_book.bids, order_book.asks))
            if len(history) > 20:
                history.pop(0)

            position = positions.get(instrument_id, 0)
            best_bid, best_ask = order_book.bids[0].price, order_book.asks[0].price
            mid_price = (best_bid + best_ask)/2

            sentiment = sentiment_scores.get(instrument_id, 0)
            short_mom = detect_momentum(history, 3)
            med_mom = detect_momentum(history, 6)
            order_flow = get_order_flow_imbalance(order_book)

            signals = {'sentiment':sentiment, 'momentum':short_mom, 'order_flow':order_flow}
            weights = signal_weights[instrument_id]
            signal_bias = sum([signals[s]*weights[s] for s in signals])

            retreat = PRICE_RETREAT_SCALE * (position / POSITION_LIMIT)**3
            fair_price = mid_price - retreat*position + signal_bias
            volatility = estimate_volatility(history, 5)*VOLATILITY_SCALE
            spread = BASE_SPREAD*(1 + abs(position)/POSITION_LIMIT + volatility)

            skew = 0
            if short_mom*med_mom > 0:
                skew += 0.5 * abs(short_mom)
            skew += order_flow
            fair_price += skew

            factor = risk_adjustment(position, volatility, POSITION_LIMIT, performance_factor)
            confidence = min(max(abs(signal_bias)+abs(skew),0.1),2.0)

            bid_price = round_down_to_tick(fair_price - spread/2, INSTRUMENTS[instrument_id].tick_size)
            ask_price = round_up_to_tick(fair_price + spread/2, INSTRUMENTS[instrument_id].tick_size)

            bid_volume = min(int(BASE_QUOTED_VOLUME*confidence*factor), max(0, POSITION_LIMIT - position))
            ask_volume = min(int(BASE_QUOTED_VOLUME*confidence*factor), max(0, POSITION_LIMIT + position))

            print(f"{instrument_id:>6s} -- ({bid_price:>6.2f}) {best_bid:>6.2f} :: {best_ask:>6.2f} ({ask_price:>6.2f}) | Vol: {bid_volume}/{ask_volume}")

            # Only place orders if volumes > 0
            if bid_volume > 0:
                tasks.append(insert_order_safe(exchange, instrument_id, bid_price, bid_volume, "bid"))
            if ask_volume > 0:
                tasks.append(insert_order_safe(exchange, instrument_id, ask_price, ask_volume, "ask"))

        if tasks:
            await asyncio.gather(*tasks)

        performance_factor = max(0.2, min(1.5, pnl/100 + 1.0))

        elapsed = (dt.datetime.now() - start_time).total_seconds()
        await asyncio.sleep(max(MAIN_LOOP_DELAY, 2 - elapsed))  # throttle

# ----------------------------
# RUN BOT
# ----------------------------
asyncio.run(market_maker_loop())
