# hedge_arbitrage.py
from structure import OrderDetail, check_breach_position_limit

POSITION_LIMIT = 100
OUTSTANDING_VOLUME_LIMIT = 200


def _book_ok(b):
    return b and b.bids and b.asks


def arbitrage_pairs(
    *,
    order_books: dict,
    positions: dict,
    outstanding_orders: dict,
    stock_dual_pairs: list[tuple[str, str]],
    hedge_order_queue,
    fair_spread: float = 0.00,      # price ratio = 1:1
    arb_edge: float = 0.05,         # need 5c mispricing to act
):
    """
    Real arb:
      if A_bid - B_ask > edge  -> sell A (at bid), buy B (at ask)
      if B_bid - A_ask > edge  -> sell B (at bid), buy A (at ask)
    Respect ±100 and 200 outstanding.
    """
    for a, b in stock_dual_pairs:
        a_book = order_books.get(a)
        b_book = order_books.get(b)
        if not (_book_ok(a_book) and _book_ok(b_book)):
            continue

        a_bid = a_book.bids[0].price
        a_ask = a_book.asks[0].price
        b_bid = b_book.bids[0].price
        b_ask = b_book.asks[0].price

        # current working
        a_outs = outstanding_orders.get(a, {})
        b_outs = outstanding_orders.get(b, {})
        a_work = sum(o.volume for o in a_outs.values())
        b_work = sum(o.volume for o in b_outs.values())

        # 1) A expensive vs B → sell A @ A_bid, buy B @ B_ask
        if (a_bid - b_ask) - fair_spread > arb_edge:
            qty = min(10,  # arbitrage size per shot
                      OUTSTANDING_VOLUME_LIMIT - a_work,
                      OUTSTANDING_VOLUME_LIMIT - b_work)
            if qty > 0:
                sell_a = OrderDetail(a, a_bid, qty, "ask", "ioc")
                buy_b  = OrderDetail(b, b_ask, qty, "bid", "ioc")

                if not check_breach_position_limit(sell_a, positions, a_outs, POSITION_LIMIT) \
                   and not check_breach_position_limit(buy_b, positions, b_outs, POSITION_LIMIT):
                    hedge_order_queue.put(sell_a)
                    hedge_order_queue.put(buy_b)
                    print(f'[ARB] A pricey: SELL {a} {qty} @ {a_bid:.2f}, BUY {b} {qty} @ {b_ask:.2f}')

        # 2) B expensive vs A → sell B @ B_bid, buy A @ A_ask
        elif (b_bid - a_ask) - fair_spread > arb_edge:
            qty = min(10,
                      OUTSTANDING_VOLUME_LIMIT - a_work,
                      OUTSTANDING_VOLUME_LIMIT - b_work)
            if qty > 0:
                sell_b = OrderDetail(b, b_bid, qty, "ask", "ioc")
                buy_a  = OrderDetail(a, a_ask, qty, "bid", "ioc")

                if not check_breach_position_limit(sell_b, positions, b_outs, POSITION_LIMIT) \
                   and not check_breach_position_limit(buy_a, positions, a_outs, POSITION_LIMIT):
                    hedge_order_queue.put(sell_b)
                    hedge_order_queue.put(buy_a)
                    print(f'[ARB] B pricey: SELL {b} {qty} @ {b_bid:.2f}, BUY {a} {qty} @ {a_ask:.2f}')
