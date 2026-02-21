# # arbitrage_only.py
# import time
# from queue import Queue
# from optibook.synchronous_client import Exchange
# from structure import OrderDetail, check_breach_position_limit

# POSITION_LIMIT = 100
# OUTSTANDING_VOLUME_LIMIT = 200
# ARB_EDGE = 0.05          # minimum edge to trade
# MAX_ARB_SIZE = 10        # per shot

# # change to your real 5 pairs
# STOCK_DUAL_PAIRS = [
#     ("NVDA", "NVDA_DUAL"),
#     ("CSCO", "CSCO_DUAL"),
#     ("ING", "ING_DUAL"),
#     ("SAN", "SAN_DUAL"),
#     ("PFE", "PFE_DUAL"),
# ]


# def book_ok(b):
#     return b and b.bids and b.asks


# def main():
#     ex = Exchange()
#     ex.connect()

#     arb_queue = Queue()

#     while True:
#         # 1) get books
#         order_books = {}
#         for a, b in STOCK_DUAL_PAIRS:
#             order_books[a] = ex.get_last_price_book(a)
#             order_books[b] = ex.get_last_price_book(b)

#         # 2) positions + outstanding
#         positions = ex.get_positions()
#         outstanding = {}
#         for a, b in STOCK_DUAL_PAIRS:
#             outstanding[a] = ex.get_outstanding_orders(a)
#             outstanding[b] = ex.get_outstanding_orders(b)

#         # 3) scan for arbitrage
#         for a, b in STOCK_DUAL_PAIRS:
#             a_book = order_books.get(a)
#             b_book = order_books.get(b)
#             if not (book_ok(a_book) and book_ok(b_book)):
#                 continue

#             a_bid = a_book.bids[0].price
#             a_ask = a_book.asks[0].price
#             b_bid = b_book.bids[0].price
#             b_ask = b_book.asks[0].price

#             a_outs = outstanding.get(a, {})
#             b_outs = outstanding.get(b, {})
#             a_work = sum(o.volume for o in a_outs.values())
#             b_work = sum(o.volume for o in b_outs.values())

#             # CASE 1: A is rich vs B → sell A @ bid, buy B @ ask
#             if (a_bid - b_ask) > ARB_EDGE:
#                 qty = min(
#                     MAX_ARB_SIZE,
#                     OUTSTANDING_VOLUME_LIMIT - a_work,
#                     OUTSTANDING_VOLUME_LIMIT - b_work,
#                 )
#                 if qty > 0:
#                     sell_a = OrderDetail(a, a_bid, qty, "ask", "ioc")
#                     buy_b = OrderDetail(b, b_ask, qty, "bid", "ioc")
#                     if (not check_breach_position_limit(sell_a, positions, a_outs, POSITION_LIMIT)
#                         and not check_breach_position_limit(buy_b, positions, b_outs, POSITION_LIMIT)):
#                         arb_queue.put(sell_a)
#                         arb_queue.put(buy_b)
#                         print(f'[ARB] A rich: SELL {a} {qty} @ {a_bid:.2f}, BUY {b} {qty} @ {b_ask:.2f}')

#             # CASE 2: B is rich vs A → sell B @ bid, buy A @ ask
#             elif (b_bid - a_ask) > ARB_EDGE:
#                 qty = min(
#                     MAX_ARB_SIZE,
#                     OUTSTANDING_VOLUME_LIMIT - a_work,
#                     OUTSTANDING_VOLUME_LIMIT - b_work,
#                 )
#                 if qty > 0:
#                     sell_b = OrderDetail(b, b_bid, qty, "ask", "ioc")
#                     buy_a = OrderDetail(a, a_ask, qty, "bid", "ioc")
#                     if (not check_breach_position_limit(sell_b, positions, b_outs, POSITION_LIMIT)
#                         and not check_breach_position_limit(buy_a, positions, a_outs, POSITION_LIMIT)):
#                         arb_queue.put(sell_b)
#                         arb_queue.put(buy_a)
#                         print(f'[ARB] B rich: SELL {b} {qty} @ {b_bid:.2f}, BUY {a} {qty} @ {a_ask:.2f}')

#         # 4) send arb orders
#         while not arb_queue.empty():
#             o: OrderDetail = arb_queue.get()
#             try:
#                 oid = ex.insert_order(
#                     instrument_id=o.instrument_id,
#                     price=o.price,
#                     volume=o.volume,
#                     side=o.side,
#                     order_type=o.order_type,
#                 )
#                 print(f'[SEND] {o.side} {o.instrument_id} {o.volume} @ {o.price:.2f} ({oid})')
#             except Exception as e:
#                 print(f'[SEND] failed: {e}')

#         time.sleep(0.1)


# if __name__ == "__main__":
#     main()
