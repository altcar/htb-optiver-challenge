import asyncio
import datetime as dt
import logging
from collections import deque
import numpy as np
import xgboost as xgb

from optibook.synchronous_client import Exchange

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

TRAIN_SECONDS = 60
TRADE_SECONDS = 300
PREDICTION_HORIZON = 3
MAX_HISTORY = 200
POSITION_LIMIT = 100

exchange = Exchange()
exchange.connect()
instruments = list(exchange.get_instruments().keys())

order_history = {inst: deque(maxlen=MAX_HISTORY) for inst in instruments}


# ------------------------------
# FEATURE & LABEL COMPUTATION
# ------------------------------
def extract_features(history):
    if len(history) < PREDICTION_HORIZON + 2:
        return None

    books = list(history)[-PREDICTION_HORIZON:]
    mids = [(b.bids[0].price + b.asks[0].price) / 2 for b in books]

    best_bid = books[-1].bids[0].price
    best_ask = books[-1].asks[0].price
    spread = best_ask - best_bid
    mid = mids[-1]

    bid_vol = sum([lvl.volume for lvl in books[-1].bids[:3]])
    ask_vol = sum([lvl.volume for lvl in books[-1].asks[:3]])
    imbalance = (bid_vol - ask_vol) / max(bid_vol + ask_vol, 1)

    micro_price = (best_bid * ask_vol + best_ask * bid_vol) / max(bid_vol + ask_vol, 1)

    ret = mids[-1] - mids[-2]
    vol = np.mean(np.abs(np.diff(mids)))

    return [mid, spread, imbalance, micro_price, ret, vol]


def compute_label(history):
    books = list(history)
    mids = [(b.bids[0].price + b.asks[0].price) / 2 for b in books]
    if len(mids) < PREDICTION_HORIZON + 1:
        return None
    future_move = mids[-1] - mids[-PREDICTION_HORIZON]
    if future_move > 0:
        return 1
    elif future_move < 0:
        return -1
    return 0


# ------------------------------
# MODEL TRAINING PHASE
# ------------------------------
async def collect_training_data():
    log.info("Collecting training data...")
    X, y = [], []
    end_time = dt.datetime.utcnow() + dt.timedelta(seconds=TRAIN_SECONDS)

    while dt.datetime.utcnow() < end_time:
        for inst in instruments:
            ob = exchange.get_last_price_book(inst)
            if ob and ob.bids and ob.asks:
                order_history[inst].append(ob)
                feat = extract_features(order_history[inst])
                label = compute_label(order_history[inst])
                if feat is not None and label is not None:
                    X.append(feat)
                    y.append(label)
        await asyncio.sleep(0.2)

    log.info(f"Collected {len(X)} samples.")
    if len(X) < 50:
        log.warning("Not enough training data.")
        return None

    # Remap labels: [-1,0,1] → [0,1,2]
    y_arr = np.array(y) + 1

    model = xgb.XGBClassifier(
        max_depth=4,
        learning_rate=0.1,
        n_estimators=80,
        subsample=0.8,
        colsample_bytree=0.8
    )
    model.fit(np.array(X), y_arr)
    log.info("Model trained.")
    return model


# ------------------------------
# TRADING PHASE
async def trade_with_model(model):
    log.info("Trading with model...")
    end_time = dt.datetime.utcnow() + dt.timedelta(seconds=TRADE_SECONDS)

    while dt.datetime.utcnow() < end_time:
        positions = exchange.get_positions() or {}
        for inst in instruments:
            ob = exchange.get_last_price_book(inst)
            if not (ob and ob.bids and ob.asks):
                continue

            order_history[inst].append(ob)
            feat = extract_features(order_history[inst])
            if feat is None:
                continue

            # Convert prediction back to [-1,0,1]
            pred = int(model.predict(np.array(feat).reshape(1, -1))[0]) - 1
            position = positions.get(inst, 0)

            target_pos = int(pred * (POSITION_LIMIT / 2))

            best_bid = ob.bids[0].price
            best_ask = ob.asks[0].price

            if target_pos > position:
                exchange.insert_order(
                    instrument_id=inst,
                    price=best_ask,
                    volume=1,
                    side="bid",
                    order_type="limit"
                )

            elif target_pos < position:
                exchange.insert_order(
                    instrument_id=inst,
                    price=best_bid,
                    volume=1,
                    side="ask",
                    order_type="limit"
                )

        await asyncio.sleep(0.5)



# ------------------------------
# MAIN LOOP
# ------------------------------
async def loop():
    while True:
        model = await collect_training_data()
        if model:
            await trade_with_model(model)

asyncio.run(loop())
