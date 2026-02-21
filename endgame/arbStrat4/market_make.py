# market_make.py
from queue import Queue
from structure import OrderDetail, check_breach_position_limit

TICK_SIZE = 0.01
POSITION_LIMIT = 100
OUTSTANDING_VOLUME_LIMIT = 200


def round_down_to_tick(price: float, tick: float) -> float:
    return (price // tick) * tick


def round_up_to_tick(price: float, tick: float) -> float:
    return ((price + 1e-9) // tick + 1) * tick if price % tick else price


def get_order_volumes(outstanding_orders_for_instr: dict) -> tuple[int, int]:
    buy_vol = 0
    sell_vol = 0
    for _, o in outstanding_orders_for_instr.items():
        if o.side == "bid":
            buy_vol += o.volume
        else:
            sell_vol += o.volume
    return buy_vol, sell_vol


def market_make(
    *,
    positions: dict,
    outstanding_orders: dict,
    order_queue: Queue,
    delete_order_queue: Queue,
    cancel_spread_threshold: float,
    list_spread: float,
    max_volume: int,
    volume_per_order: int,
    stock_id: str,
    incent_price: float,
    best_bid: float,
    best_ask: float,
):
    orders_for_instr = outstanding_orders.get(stock_id, {})
    current_buy_volume, current_sell_volume = get_order_volumes(orders_for_instr)
    current_total_outstanding = current_buy_volume + current_sell_volume

    # cancel stale faster
    for order_id, order in orders_for_instr.items():
        if order.side == "bid" and (order.price > incent_price or order.price < incent_price - cancel_spread_threshold):
            delete_order_queue.put((stock_id, order_id))
        elif order.side == "ask" and (order.price < incent_price or order.price > incent_price + cancel_spread_threshold):
            delete_order_queue.put((stock_id, order_id))

    # BID
    if current_total_outstanding < OUTSTANDING_VOLUME_LIMIT and current_buy_volume < max_volume:
        # our theoretical bid
        theo_bid = round_down_to_tick(incent_price - list_spread, TICK_SIZE)
        # be competitive: try to join or step in front of market if inside our band
        if best_bid is not None and best_bid >= theo_bid and best_bid <= incent_price:
            bid_price = round_up_to_tick(best_bid + 0.0 * TICK_SIZE, TICK_SIZE)  # join
        elif best_bid is not None and best_bid + TICK_SIZE <= incent_price:
            bid_price = round_up_to_tick(best_bid + TICK_SIZE, TICK_SIZE)  # step ahead 1 tick
        else:
            bid_price = theo_bid

        inv = positions.get(stock_id, 0)
        raw_allowed = max_volume - current_buy_volume - inv - 1
        allowed = max(0, raw_allowed)
        leftover_outstanding = OUTSTANDING_VOLUME_LIMIT - current_total_outstanding
        allowed = min(allowed, leftover_outstanding)
        volume = min(volume_per_order, allowed)

        if volume > 0:
            order = OrderDetail(
                instrument_id=stock_id,
                price=bid_price,
                volume=volume,
                side="bid",
                order_type="limit",
            )
            if not check_breach_position_limit(order, positions, orders_for_instr, POSITION_LIMIT):
                order_queue.put(order)

    # ASK
    if current_total_outstanding < OUTSTANDING_VOLUME_LIMIT and current_sell_volume < max_volume:
        theo_ask = round_up_to_tick(incent_price + list_spread, TICK_SIZE)
        if best_ask is not None and best_ask <= theo_ask and best_ask >= incent_price:
            ask_price = round_down_to_tick(best_ask - 0.0 * TICK_SIZE, TICK_SIZE)  # join
        elif best_ask is not None and best_ask - TICK_SIZE >= incent_price:
            ask_price = round_down_to_tick(best_ask - TICK_SIZE, TICK_SIZE)        # step ahead 1 tick
        else:
            ask_price = theo_ask

        inv = positions.get(stock_id, 0)
        raw_allowed = max_volume - current_sell_volume + inv - 1
        allowed = max(0, raw_allowed)
        leftover_outstanding = OUTSTANDING_VOLUME_LIMIT - current_total_outstanding
        allowed = min(allowed, leftover_outstanding)
        volume = min(volume_per_order, allowed)

        if volume > 0:
            order = OrderDetail(
                instrument_id=stock_id,
                price=ask_price,
                volume=volume,
                side="ask",
                order_type="limit",
            )
            if not check_breach_position_limit(order, positions, orders_for_instr, POSITION_LIMIT):
                order_queue.put(order)
