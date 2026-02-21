# controller.py
import time
from queue import Queue
from optibook.synchronous_client import Exchange

from dual_strategy import dual_strategy_multi
from structure import OrderDetail

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

    # state we maintain
    positions: dict[str, int] = {}
    outstanding_orders: dict[str, dict] = {}

    # queues used by market_make
    order_queue: Queue = Queue()
    delete_order_queue: Queue = Queue()

    # main loop
    while True:
        # 1) refresh books for ALL instruments we care about
        order_books = {}
        for stock_id, hedge_stock_id in STOCK_DUAL_PAIRS:
            order_books[stock_id] = exchange.get_last_price_book(stock_id)
            order_books[hedge_stock_id] = exchange.get_last_price_book(hedge_stock_id)

        # 2) refresh positions
        # (optibook: get_positions returns dict)
        positions = exchange.get_positions()

        # 3) refresh outstanding (per instrument)
        # we’ll store as {instrument: {order_id: order}}
        outstanding_orders = {}
        for stock_id, hedge_stock_id in STOCK_DUAL_PAIRS:
            outstanding_orders[stock_id] = exchange.get_outstanding_orders(stock_id)

        # 4) run the 5-way dual strategy
        dual_strategy_multi(
            order_books=order_books,
            outstanding_orders=outstanding_orders,
            positions=positions,
            order_queue=order_queue,
            delete_order_queue=delete_order_queue,
            stock_dual_pairs=STOCK_DUAL_PAIRS,
            default_list_spread=0.15,
            default_cancel_spread_threshold=0.30,
            default_max_volume=100,
            default_volume_per_order=20,
            position_limit=100,
        )

        # 5) send orders the strategy asked for
        while not order_queue.empty():
            order: OrderDetail = order_queue.get()
            try:
                order_id = exchange.insert_order(
                    instrument_id=order.instrument_id,
                    price=order.price,
                    volume=order.volume,
                    side=order.side,
                    order_type=order.order_type,
                )
                time.sleep(0.3)
                print(f'>>> Sent {order.side} {order.instrument_id} {order.volume} @ {order.price:.2f} (id={order_id})')
            except Exception as e:
                print(f'Failed to insert order: {e}')

        # 6) send cancellations
        while not delete_order_queue.empty():
            instrument_id, order_id = delete_order_queue.get()
            try:
                exchange.delete_order(instrument_id = instrument_id, order_id = order_id)
                print(f'>>> Cancelled {instrument_id} order {order_id}')
            except Exception as e:
                print(f'Failed to cancel order {order_id} on {instrument_id}: {e}')

        # 7) small sleep to avoid hammering
        time.sleep(0.3)


if __name__ == "__main__":
    main()
