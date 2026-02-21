# controller.py
import time
from queue import Queue
from structure import OrderDetail
from optibook.synchronous_client import Exchange

from dual_strategy import dual_strategy_multi
from structure import OrderDetail, check_breach_position_limit
from hedge_strategy import hedge_strategy

"""
CONTROLLER ASSUMPTIONS
- there are 5 stocks and 5 duals
- we only run dual-strategy MM
- no ETFs / futures / options
"""

# TODO: put your real instrument names here:
STOCK_DUAL_PAIRS = [
    ("NVDA", "NVDA_DUAL"),
    ("CSCO", "CSCO_DUAL"),
    ("ING", "ING_DUAL"),
    ("SAN", "SAN_DUAL"),
    ("PFE", "PFE_DUAL"),
]



POSITION_LIMIT = 100
OUTSTANDING_VOLUME_LIMIT = 200
HEDGE_SPREAD = 0.10   # how far from mid we hedge


def mid_from_book(book):
    if not (book and book.bids and book.asks):
        return None
    return (book.bids[0].price + book.asks[0].price) / 2


def main():
    exchange = Exchange()
    exchange.connect()

    order_queue = Queue()
    delete_order_queue = Queue()

    # remember positions to detect changes
    prev_positions = {}

    while True:
        # 1) get books
        order_books = {}
        for trade_id, hedge_id in STOCK_DUAL_PAIRS:
            order_books[trade_id] = exchange.get_last_price_book(trade_id)
            time.sleep(1/20)
            order_books[hedge_id] = exchange.get_last_price_book(hedge_id)
            time.sleep(1/20)

        # 2) positions for MM
        positions = exchange.get_positions()
        if not prev_positions:
            prev_positions = dict(positions)

        # 3) outstanding for MM
        outstanding_orders = {}
        for trade_id, hedge_id in STOCK_DUAL_PAIRS:
            outstanding_orders[trade_id] = exchange.get_outstanding_orders(trade_id)
            outstanding_orders[hedge_id] = exchange.get_outstanding_orders(hedge_id)

        # 4) run MM
        dual_strategy_multi(
            order_books=order_books,
            outstanding_orders=outstanding_orders,
            positions=positions,
            order_queue=order_queue,
            delete_order_queue=delete_order_queue,
            stock_dual_pairs=STOCK_DUAL_PAIRS,
            default_list_spread=0.15,
            default_cancel_spread_threshold=0.30,
            default_max_volume=80,
            default_volume_per_order=20,
            position_limit=POSITION_LIMIT,
        )

        # 5) send MM orders
        while not order_queue.empty():
            order: OrderDetail = order_queue.get()
            try:
                oid = exchange.insert_order(
                    instrument_id=order.instrument_id,
                    price=order.price,
                    volume=order.volume,
                    side=order.side,
                    order_type=order.order_type,
                )
                time.sleep(1/25)
                print(f'[MM] sent {order.side} {order.instrument_id} {order.volume} @ {order.price:.2f} ({oid})')
            except Exception as e:
                print(f'[MM] failed to insert: {e}')

        # 6) send MM cancels
        while not delete_order_queue.empty():
            instr_id, oid = delete_order_queue.get()
            try:
                exchange.delete_order(instrument_id = instr_id,order_id = oid)
                print(f'[MM] cancelled {instr_id} {oid}')
            except Exception as e:
                print(f'[MM] failed cancel {instr_id} {oid}: {e}')

        # 7) refresh to see fills
        positions_now = exchange.get_positions()
        outstanding_now = {}
        for trade_id, hedge_id in STOCK_DUAL_PAIRS:
            outstanding_now[trade_id] = exchange.get_outstanding_orders(trade_id)
            outstanding_now[hedge_id] = exchange.get_outstanding_orders(hedge_id)

        # 8) per-trade hedging (both directions)
        for trade_id, hedge_id in STOCK_DUAL_PAIRS:
            old_pos = prev_positions.get(trade_id, 0)
            new_pos = positions_now.get(trade_id, 0)
            delta = new_pos - old_pos

            if delta == 0:
                continue

            hedge_book = order_books.get(hedge_id)
            mid = mid_from_book(hedge_book)
            if mid is None:
                print(f'[HEDGE-PER-TRADE] no book for {hedge_id}, skip')
                continue

            hedge_outs = outstanding_now.get(hedge_id, {})
            current_working = sum(o.volume for o in hedge_outs.values())

            # we bought trade_id → sell hedge_id
            if delta > 0:
                if current_working + delta > OUTSTANDING_VOLUME_LIMIT:
                    print(f'[HEDGE-PER-TRADE] {hedge_id} outstanding limit hit, skip sell')
                else:
                    price = mid - HEDGE_SPREAD
                    hedge_order = OrderDetail(
                        instrument_id=hedge_id,
                        price=price,
                        volume=delta,
                        side="ask",
                        order_type="ioc",
                    )
                    if not check_breach_position_limit(hedge_order, positions_now, hedge_outs, POSITION_LIMIT):
                        try:
                            oid = exchange.insert_order(
                                instrument_id=hedge_order.instrument_id,
                                price=hedge_order.price,
                                volume=hedge_order.volume,
                                side=hedge_order.side,
                                order_type=hedge_order.order_type,
                            )
                            time.sleep(1/25)
                            print(f'[HEDGE-PER-TRADE] sell {hedge_id} {delta} @ {price:.2f} ({oid})')
                        except Exception as e:
                            print(f'[HEDGE-PER-TRADE] failed sell: {e}')
                    else:
                        print(f'[HEDGE-PER-TRADE] sell would breach pos limit on {hedge_id}, skip')

            # we sold trade_id → buy hedge_id
            if delta < 0:
                buy_size = -delta  # positive
                if current_working + buy_size > OUTSTANDING_VOLUME_LIMIT:
                    print(f'[HEDGE-PER-TRADE] {hedge_id} outstanding limit hit, skip buy')
                else:
                    price = mid + HEDGE_SPREAD
                    hedge_order = OrderDetail(
                        instrument_id=hedge_id,
                        price=price,
                        volume=buy_size,
                        side="bid",
                        order_type="ioc",
                    )
                    if not check_breach_position_limit(hedge_order, positions_now, hedge_outs, POSITION_LIMIT):
                        try:
                            oid = exchange.insert_order(
                                instrument_id=hedge_order.instrument_id,
                                price=hedge_order.price,
                                volume=hedge_order.volume,
                                side=hedge_order.side,
                                order_type=hedge_order.order_type,
                            )
                            time.sleep(1/25)
                            print(f'[HEDGE-PER-TRADE] buy {hedge_id} {buy_size} @ {price:.2f} ({oid})')
                        except Exception as e:
                            print(f'[HEDGE-PER-TRADE] failed buy: {e}')
                    else:
                        print(f'[HEDGE-PER-TRADE] buy would breach pos limit on {hedge_id}, skip')

        # 9) update prev_positions
        prev_positions = positions_now

        # 10) sleep
        time.sleep(0.3)


if __name__ == "__main__":
    main()


