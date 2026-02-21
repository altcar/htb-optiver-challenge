# controller.py
import time
from queue import Queue
from structure import OrderDetail
from optibook.synchronous_client import Exchange

from dual_strategy import dual_strategy_multi
from structure import OrderDetail, check_breach_position_limit
from hedge_arbitrage import arbitrage_pairs

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
HEDGE_SPREAD = 0.5   # how far from mid we hedge


def mid_from_book(book):
    if not (book and book.bids and book.asks):
        return None
    return (book.bids[0].price + book.asks[0].price) / 2


def main():
    exchange = Exchange()
    exchange.connect()

    # queues for MM and arb
    mm_order_queue = Queue()
    mm_delete_queue = Queue()
    arb_order_queue = Queue()

    while True:
        # 1) get all order books (both legs)
        order_books = {}
        for a, b in STOCK_DUAL_PAIRS:
            order_books[a] = exchange.get_last_price_book(a)
            time.sleep(1/25)
            order_books[b] = exchange.get_last_price_book(b)
            time.sleep(1/25)

        # 2) snapshot positions & outstanding for MM
        positions = exchange.get_positions()
        outstanding = {}
        for a, b in STOCK_DUAL_PAIRS:
            outstanding[a] = exchange.get_outstanding_orders(a)
            outstanding[b] = exchange.get_outstanding_orders(b)

        # 3) run dual-priced market making on the 5 main legs
        dual_strategy_multi(
            order_books=order_books,
            outstanding_orders=outstanding,
            positions=positions,
            order_queue=mm_order_queue,
            delete_order_queue=mm_delete_queue,
            stock_dual_pairs=STOCK_DUAL_PAIRS,
        )
        time.sleep(1/25)

        # 4) send MM orders
        while not mm_order_queue.empty():
            o: OrderDetail = mm_order_queue.get()
            try:
                oid = exchange.insert_order(
                    instrument_id=o.instrument_id,
                    price=o.price,
                    volume=o.volume,
                    side=o.side,
                    order_type=o.order_type,
                )
                print(f'[MM] {o.side} {o.instrument_id} {o.volume} @ {o.price:.2f} ({oid})')
            except Exception as e:
                print(f'[MM] insert failed: {e}')
        time.sleep(2/25)

        # 5) send MM cancels
        while not mm_delete_queue.empty():
            instr_id, oid = mm_delete_queue.get()
            try:
                exchange.delete_order(instrument_id = instr_id, order_id = oid)
                print(f'[MM] cancelled {instr_id} {oid}')
            except Exception as e:
                print(f'[MM] cancel failed: {e}')
        time.sleep(2/25)

        # 6) refresh after MM so arb sees latest fills
        positions = exchange.get_positions()
        outstanding = {}
        for a, b in STOCK_DUAL_PAIRS:
            outstanding[a] = exchange.get_outstanding_orders(a)
            outstanding[b] = exchange.get_outstanding_orders(b)

        # 7) run arbitrage/hedging separately (buy cheap leg, sell expensive leg)
        arbitrage_pairs(
            order_books=order_books,
            positions=positions,
            outstanding_orders=outstanding,
            stock_dual_pairs=STOCK_DUAL_PAIRS,
            hedge_order_queue=arb_order_queue,
            arb_edge=0.05,  # tune this
        )

        # 8) send arb/hedge orders
        while not arb_order_queue.empty():
            h: OrderDetail = arb_order_queue.get()
            try:
                oid = exchange.insert_order(
                    instrument_id=h.instrument_id,
                    price=h.price,
                    volume=h.volume,
                    side=h.side,
                    order_type=h.order_type,
                )
                print(f'[ARB] {h.side} {h.instrument_id} {h.volume} @ {h.price:.2f} ({oid})')
            except Exception as e:
                print(f'[ARB] insert failed: {e}')

        # 9) run faster to stay competitive
        time.sleep(0.3)


if __name__ == "__main__":
    main()






