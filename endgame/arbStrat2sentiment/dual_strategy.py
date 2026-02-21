# dual_strategy.py
from market_make import market_make

def dual_strategy_single(
    order_book,
    hedge_order_book,
    outstanding_orders,
    positions,
    order_queue,
    delete_order_queue,
    stock_id,
    hedge_stock_id,
    list_spread,
    cancel_spread_threshold,
    max_volume,
    volume_per_order,
    position_limit=100,
    changes={}
):
    # need two-sided books
    if not (hedge_order_book and hedge_order_book.bids and hedge_order_book.asks):
        # print(f'[{stock_id}] hedge {hedge_stock_id} not two-sided → skip')
        return
    if not (order_book and order_book.bids and order_book.asks):
        # print(f'[{stock_id}] own book not two-sided → skip')
        return

    # (for debug)
    best_bid_price = order_book.bids[0].price
    best_ask_price = order_book.asks[0].price
    print(f'{stock_id} L1: {best_bid_price:.2f} :: {best_ask_price:.2f}')

    dual_best_bid_price = hedge_order_book.bids[0].price
    dual_best_ask_price = hedge_order_book.asks[0].price
    # print(f'{hedge_stock_id} L1: {dual_best_bid_price:.2f} :: {dual_best_ask_price:.2f}')

    # fair from hedge’s mid
    expected_price = (dual_best_bid_price + dual_best_ask_price) / 2
    expected_price += (changes[stock_id] * expected_price)

    # inventory nudge
    incent = -positions.get(stock_id, 0) / float(position_limit)
    incent_price = expected_price + incent * list_spread * 0.9

    market_make(
        positions=positions,
        outstanding_orders={stock_id: outstanding_orders},
        order_queue=order_queue,
        delete_order_queue=delete_order_queue,
        cancel_spread_threshold=cancel_spread_threshold,
        list_spread=list_spread,
        max_volume=max_volume,
        volume_per_order=volume_per_order,
        stock_id=stock_id,
        incent_price=incent_price,
    )


def dual_strategy_multi(
    *,
    order_books: dict,
    outstanding_orders: dict,
    positions: dict,
    order_queue,
    delete_order_queue,
    stock_dual_pairs: list[tuple[str, str]],
    default_list_spread: float = 0.15,
    default_cancel_spread_threshold: float = 0.30,
    default_max_volume: int = 100,
    default_volume_per_order: int = 20,
    position_limit: int = 100,
    per_instrument_overrides: dict | None = None,
    changes: dict
):
    if per_instrument_overrides is None:
        per_instrument_overrides = {}

    for stock_id, hedge_stock_id in stock_dual_pairs:
        ob = order_books.get(stock_id)
        hob = order_books.get(hedge_stock_id)
        outs = outstanding_orders.get(stock_id, {})

        ovr = per_instrument_overrides.get(stock_id, {})
        list_spread = ovr.get("list_spread", default_list_spread)
        cancel_spread_threshold = ovr.get("cancel_spread_threshold", default_cancel_spread_threshold)
        max_volume = ovr.get("max_volume", default_max_volume)
        volume_per_order = ovr.get("volume_per_order", default_volume_per_order)

        # print(f'---- dual run: {stock_id} vs {hedge_stock_id} ----')
        dual_strategy_single(
            order_book=ob,
            hedge_order_book=hob,
            outstanding_orders=outs,
            positions=positions,
            order_queue=order_queue,
            delete_order_queue=delete_order_queue,
            stock_id=stock_id,
            hedge_stock_id=hedge_stock_id,
            list_spread=list_spread,
            cancel_spread_threshold=cancel_spread_threshold,
            max_volume=max_volume,
            volume_per_order=volume_per_order,
            position_limit=position_limit,
            changes=changes
        )
