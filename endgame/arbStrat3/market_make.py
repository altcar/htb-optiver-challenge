# market_make.py
from queue import Queue
from structure import OrderDetail, check_breach_position_limit

TICK_SIZE = 0.01
POSITION_LIMIT = 100
OUTSTANDING_VOLUME_LIMIT = 200   # total working (bids + asks) per instrument


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
):
    # current working orders
    orders_for_instr = outstanding_orders.get(stock_id, {})
    current_buy_volume, current_sell_volume = get_order_volumes(orders_for_instr)
    current_total_outstanding = current_buy_volume + current_sell_volume

    # 1) cancel off-centre orders
    for order_id, order in orders_for_instr.items():
        if order.side == "bid" and (order.price > incent_price or order.price < incent_price - cancel_spread_threshold):
            print(f'Cancelling bid {order_id} {stock_id} @ {order.price:.2f}')
            delete_order_queue.put((stock_id, order_id))
        elif order.side == "ask" and (order.price < incent_price or order.price > incent_price + cancel_spread_threshold):
            print(f'Cancelling ask {order_id} {stock_id} @ {order.price:.2f}')
            delete_order_queue.put((stock_id, order_id))

    # 2) BID side
    if current_total_outstanding < OUTSTANDING_VOLUME_LIMIT and current_buy_volume < max_volume:
        bid_price = round_down_to_tick(incent_price - list_spread, TICK_SIZE)
        inv = positions.get(stock_id, 0)

        # inventory-aware cap
        raw_allowed = max_volume - current_buy_volume - inv - 1
        allowed = max(0, raw_allowed)

        # also respect outstanding cap
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
                print(f'Insert bid {stock_id}: {volume} @ {bid_price:.2f}')
                order_queue.put(order)
            else:
                print(f'Not inserting bid for {stock_id} (position limit).')

    # 3) ASK side
    # recompute current totals? we can reuse (slightly conservative)
    if current_total_outstanding < OUTSTANDING_VOLUME_LIMIT and current_sell_volume < max_volume:
        ask_price = round_up_to_tick(incent_price + list_spread, TICK_SIZE)
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
                print(f'Insert ask {stock_id}: {volume} @ {ask_price:.2f}')
                order_queue.put(order)
            else:
                print(f'Not inserting ask for {stock_id} (position limit).')
