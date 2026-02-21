# controller.py
import time
from queue import Queue
from optibook.synchronous_client import Exchange

from dual_strategy import dual_strategy_multi
from structure import OrderDetail
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



def main():
    exchange = Exchange()
    exchange.connect()

    # queues
    order_queue = Queue()
    delete_order_queue = Queue()
    hedge_order_queue = Queue()

    while True:
        # 1) get books for all 10 instruments
        order_books = {}
        for trade_id, hedge_id in STOCK_DUAL_PAIRS:
            order_books[trade_id] = exchange.get_last_price_book(trade_id)
            order_books[hedge_id] = exchange.get_last_price_book(hedge_id)

        # 2) snapshot positions (used by MM)
        positions = exchange.get_positions()

        # 3) snapshot outstanding (used by MM)
        outstanding_orders = {}
        for trade_id, hedge_id in STOCK_DUAL_PAIRS:
            outstanding_orders[trade_id] = exchange.get_outstanding_orders(trade_id)
            outstanding_orders[hedge_id] = exchange.get_outstanding_orders(hedge_id)

        # 4) MARKET MAKING (creates + cancels)
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
            position_limit=100,
        )

        # 5) SEND MM ORDERS
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
                print(f'[MM] sent {order.side} {order.instrument_id} {order.volume} @ {order.price:.2f} ({oid})')
            except Exception as e:
                print(f'[MM] failed to insert: {e}')

        # 6) SEND MM CANCELS
        while not delete_order_queue.empty():
            instr_id, oid = delete_order_queue.get()
            try:
                exchange.delete_order(instrument_id = instr_id,order_id = oid)
                print(f'[MM] cancelled {instr_id} {oid}')
            except Exception as e:
                print(f'[MM] failed cancel {instr_id} {oid}: {e}')

        # 7) *** REFRESH STATE FOR HEDGING ***
        # now we re-pull positions & outstanding so hedge sees fills from MM
        positions = exchange.get_positions()
        outstanding_orders = {}
        for trade_id, hedge_id in STOCK_DUAL_PAIRS:
            outstanding_orders[trade_id] = exchange.get_outstanding_orders(trade_id)
            outstanding_orders[hedge_id] = exchange.get_outstanding_orders(hedge_id)

        # 8) HEDGING (portfolio over the 5 hedge names)
        mm_names = [a for (a, _) in STOCK_DUAL_PAIRS]
        hedge_names = [b for (_, b) in STOCK_DUAL_PAIRS]

        hedge_strategy(
            order_books=order_books,
            mm_stocks=mm_names,
            hedge_stocks=hedge_names,
            outstanding_orders=outstanding_orders,
            positions=positions,
            hedge_order_queue=hedge_order_queue,
            hedge_spread=0.10,
            position_limit=100,
        )

        # 9) SEND HEDGE ORDERS
        while not hedge_order_queue.empty():
            order: OrderDetail = hedge_order_queue.get()
            try:
                oid = exchange.insert_order(
                    instrument_id=order.instrument_id,
                    price=order.price,
                    volume=order.volume,
                    side=order.side,
                    order_type=order.order_type,
                )
                print(f'[HEDGE] sent {order.side} {order.instrument_id} {order.volume} @ {order.price:.2f} ({oid})')
            except Exception as e:
                print(f'[HEDGE] failed: {e}')

        # 10) small sleep
        time.sleep(0.3)


if __name__ == "__main__":
    main()