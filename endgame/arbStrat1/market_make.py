# market_make.py
from queue import Queue
from structure import OrderDetail, check_breach_position_limit

# adjust if your sim uses a different tick
TICK_SIZE = 0.01


def round_down_to_tick(price: float, tick: float) -> float:
    return (price // tick) * tick


def round_up_to_tick(price: float, tick: float) -> float:
    # little trick to avoid FP issues
    return ((price + 1e-9) // tick + 1) * tick if price % tick else price


def get_order_volumes(outstanding_orders_for_instr: dict) -> tuple[int, int]:
    """
    Sum of current working bid volume, and current working ask volume.
    """
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
):
    """
    Keep one instrument quoted around `incent_price` with inventory-aware sizes.
    """
    # 1) how much we already have working
    current_buy_volume, current_sell_volume = get_order_volumes(outstanding_orders.get(stock_id, {}))

    # 2) cancel stale / off-centre orders
    orders = outstanding_orders.get(stock_id, {})
    for order_id, order in orders.items():
        if order.side == 'bid' and (order.price > incent_price or order.price < incent_price - cancel_spread_threshold):
            print(f'Cancelling bid order {order_id} for {stock_id} at price {order.price:.2f}.')
            delete_order_queue.put((stock_id, order_id))

        elif order.side == 'ask' and (order.price < incent_price or order.price > incent_price + cancel_spread_threshold):
            print(f'Cancelling ask order {order_id} for {stock_id} at price {order.price:.2f}.')
            delete_order_queue.put((stock_id, order_id))

    # 3) place / top-up BID
    if current_buy_volume < max_volume:
        bid_price = round_down_to_tick(incent_price - list_spread, TICK_SIZE)
        side = 'bid'

        # inventory-aware bid size
        inv = positions.get(stock_id, 0)
        raw_allowed = max_volume - current_buy_volume - inv - 1
        allowed = max(0, raw_allowed)
        volume = min(volume_per_order, allowed)

        if volume > 0:
            order = OrderDetail(
                instrument_id=stock_id,
                price=bid_price,
                volume=volume,
                side=side,
                order_type='limit'
            )
            if not check_breach_position_limit(order, positions, outstanding_orders.get(stock_id, {})):
                print(f'Inserting {side} for {stock_id}: {volume} @ {bid_price:.2f}')
                order_queue.put(order)
            else:
                print(f'Not inserting {volume} {side} for {stock_id} to avoid position-limit breach.')

    # 4) place / top-up ASK
    if current_sell_volume < max_volume:
        ask_price = round_up_to_tick(incent_price + list_spread, TICK_SIZE)
        side = 'ask'

        inv = positions.get(stock_id, 0)
        raw_allowed = max_volume - current_sell_volume + inv - 1
        allowed = max(0, raw_allowed)
        volume = min(volume_per_order, allowed)

        if volume > 0:
            order = OrderDetail(
                instrument_id=stock_id,
                price=ask_price,
                volume=volume,
                side=side,
                order_type='limit'
            )
            if not check_breach_position_limit(order, positions, outstanding_orders.get(stock_id, {})):
                print(f'Inserting {side} for {stock_id}: {volume} @ {ask_price:.2f}')
                order_queue.put(order)
            else:
                print(f'Not inserting {volume} {side} for {stock_id} to avoid position-limit breach.')
