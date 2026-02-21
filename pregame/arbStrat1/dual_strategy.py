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
):
    """
    Original dual-strategy logic, for ONE stock + ONE dual.
    """

    stock_order_book = order_book
    hedge_stock_order_book = hedge_order_book

    # need both sides in BOTH books
    if not (hedge_stock_order_book and hedge_stock_order_book.bids and hedge_stock_order_book.asks):
        print(f'[{stock_id}] Hedge book {hedge_stock_id} has no bids/asks. Skipping.')
        return

    if not (stock_order_book and stock_order_book.bids and stock_order_book.asks):
        print(f'[{stock_id}] Own book has no bids/asks. Skipping.')
        return

    # top of book
    best_bid_price = stock_order_book.bids[0].price
    best_ask_price = stock_order_book.asks[0].price
    print(f'Top level prices for {stock_id}: {best_bid_price:.2f} :: {best_ask_price:.2f}')

    dual_best_bid_price = hedge_stock_order_book.bids[0].price
    dual_best_ask_price = hedge_stock_order_book.asks[0].price
    print(f'Top level prices for dual-{hedge_stock_id}: {dual_best_bid_price:.2f} :: {dual_best_ask_price:.2f}')

    # implied from dual
    expected_price = (dual_best_bid_price + dual_best_ask_price) / 2
    print(f'Expected price from {hedge_stock_id}: {expected_price:.2f}')

    # inventory nudge
    incent = - positions.get(stock_id, 0) / float(position_limit)
    incent_price = expected_price + incent * list_spread * 0.9

    # send to MM engine
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
    default_volume_per_order: int = 50,
    position_limit: int = 100,
    per_instrument_overrides: dict | None = None,
):
    """
    Run the dual-strategy for N (here: 5) instruments.

    order_books: {instrument_id: price_book}
    outstanding_orders: {instrument_id: {order_id: order}}
    positions: {instrument_id: int}
    """
    if per_instrument_overrides is None:
        per_instrument_overrides = {}

    for stock_id, hedge_stock_id in stock_dual_pairs:
        ob = order_books.get(stock_id)
        hob = order_books.get(hedge_stock_id)
        outs = outstanding_orders.get(stock_id, {})

        # per-instrument tuning
        o = per_instrument_overrides.get(stock_id, {})
        list_spread = o.get("list_spread", default_list_spread)
        cancel_spread_threshold = o.get("cancel_spread_threshold", default_cancel_spread_threshold)
        max_volume = o.get("max_volume", default_max_volume)
        volume_per_order = o.get("volume_per_order", default_volume_per_order)

        print(f'---- running dual for {stock_id} vs {hedge_stock_id} ----')
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
        )
