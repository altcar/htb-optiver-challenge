# hedge_strategy.py
from structure import OrderDetail, check_breach_position_limit

TICK_SIZE = 0.01
POSITION_LIMIT = 100
OUTSTANDING_VOLUME_LIMIT = 200   # per instrument


def _mid_from_book(book):
    if not (book and book.bids and book.asks):
        return None
    return (book.bids[0].price + book.asks[0].price) / 2


def hedge_strategy(
    *,
    order_books: dict,
    mm_stocks: list[str],
    hedge_stocks: list[str],
    outstanding_orders: dict,
    positions: dict,
    hedge_order_queue,
    hedge_spread: float = 0.10,
    position_limit: int = POSITION_LIMIT,
):
    """
    Portfolio-style hedging for N traded names vs N hedge names (stocks only).
    - position cap: ±100 per instrument
    - outstanding cap: 200 per instrument
    """
    # 1) delta from the things we’re trading
    primary_delta = 0
    for instr in mm_stocks:
        primary_delta += positions.get(instr, 0)

    # 2) delta from the instruments we use to hedge
    hedge_delta = 0
    for instr in hedge_stocks:
        hedge_delta += positions.get(instr, 0)

    # 3) how much to offset
    net_to_hedge = -(primary_delta + hedge_delta)

    if net_to_hedge == 0:
        return

    print(f'[HEDGE] net delta to hedge: {net_to_hedge}')

    remaining = net_to_hedge

    for instr in hedge_stocks:
        if remaining == 0:
            break

        book = order_books.get(instr)
        mid = _mid_from_book(book)
        if mid is None:
            print(f'[HEDGE] no book for {instr}, skip')
            continue

        current_pos = positions.get(instr, 0)
        orders_for_instr = outstanding_orders.get(instr, {})
        current_working = sum(o.volume for o in orders_for_instr.values())

        # respect outstanding cap
        if current_working >= OUTSTANDING_VOLUME_LIMIT:
            print(f'[HEDGE] {instr} at outstanding limit ({current_working}), skip')
            continue

        if remaining > 0:
            # we need to BUY
            max_buyable = position_limit - current_pos
            vol = min(remaining, max_buyable)
            # also respect outstanding cap
            vol = min(vol, OUTSTANDING_VOLUME_LIMIT - current_working)
            vol = max(0, vol)

            if vol > 0:
                price = mid + hedge_spread
                order = OrderDetail(
                    instrument_id=instr,
                    price=price,
                    volume=vol,
                    side="bid",
                    order_type="ioc",
                )
                if not check_breach_position_limit(order, positions, orders_for_instr, position_limit):
                    print(f'[HEDGE] buy {instr} {vol} @ {price:.2f}')
                    hedge_order_queue.put(order)
                    remaining -= vol
                else:
                    print(f'[HEDGE] buy {instr} would breach position limit → skip')

        else:
            # we need to SELL (remaining < 0)
            need_to_sell = -remaining
            max_sellable = position_limit + current_pos   # if current_pos negative, this shrinks
            vol = min(need_to_sell, max_sellable)
            vol = min(vol, OUTSTANDING_VOLUME_LIMIT - current_working)
            vol = max(0, vol)

            if vol > 0:
                price = mid - hedge_spread
                order = OrderDetail(
                    instrument_id=instr,
                    price=price,
                    volume=vol,
                    side="ask",
                    order_type="ioc",
                )
                if not check_breach_position_limit(order, positions, orders_for_instr, position_limit):
                    print(f'[HEDGE] sell {instr} {vol} @ {price:.2f}')
                    hedge_order_queue.put(order)
                    remaining += vol
                else:
                    print(f'[HEDGE] sell {instr} would breach position limit → skip')
