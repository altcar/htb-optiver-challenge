import datetime as dt
import time
import random
import logging

from optibook.synchronous_client import Exchange

exchange = Exchange()
exchange.connect()

logging.getLogger('client').setLevel('ERROR')

def trade_would_breach_position_limit(instrument_id, volume, side, position_limit=100):
    positions = exchange.get_positions()
    position_instrument = positions[instrument_id]

    if side == 'bid':
        return position_instrument + volume > position_limit
    elif side == 'ask':
        return position_instrument - volume < -position_limit
    else:
        raise Exception(f'''Invalid side provided: {side}, expecting 'bid' or 'ask'.''')


def print_positions_and_pnl(always_display=None):
    positions = exchange.get_positions()
    print('Positions:')
    for instrument_id in positions:
        if not always_display or instrument_id in always_display or positions[instrument_id] != 0:
            print(f'  {instrument_id:20s}: {positions[instrument_id]:4.0f}')

    pnl = exchange.get_pnl()
    if pnl:
        print(f'\nPnL: {pnl:.2f}')


STOCK_A_IDS = ['NVDA']
STOCK_B_IDS = ['NVDA_DUAL']
TIME_SLEEP = 0.05

while True:
    for STOCK_A_ID, STOCK_B_ID in zip(STOCK_A_IDS, STOCK_B_IDS):
        time.sleep(TIME_SLEEP)

        order_book_a = exchange.get_last_price_book(STOCK_A_ID)
        order_book_b = exchange.get_last_price_book(STOCK_B_ID)

        if not (order_book_a and order_book_a.bids and order_book_a.asks):
            print(f'Order book for {STOCK_A_ID} does not have bids or offers. Skipping iteration.')
            continue

        if not(order_book_b and order_book_b.bids and order_book_b.asks):
            print(f'Order book for {STOCK_B_ID} does not have bids or offers. Skipping iteration.')
            continue

        print(f'')
        print(f'-----------------------------------------------------------------')
        print(f'TRADE LOOP ITERATION ENTERED AT {str(dt.datetime.now()):18s} UTC.')
        print(f'-----------------------------------------------------------------')

        print_positions_and_pnl(always_display=[STOCK_A_ID, STOCK_B_ID])
        print(f'')


        best_bid_price_a = order_book_a.bids[0].price
        best_ask_price_a = order_book_a.asks[0].price
        best_bid_price_b = order_book_b.bids[0].price
        best_ask_price_b = order_book_b.asks[0].price

        if best_ask_price_a < best_bid_price_b:
            buy_stock_id = STOCK_A_ID
            buy_price = best_ask_price_a
            sell_stock_id = STOCK_B_ID
            sell_price = best_bid_price_b
            order_book_buy = order_book_a
            order_book_sell = order_book_b
        elif best_ask_price_b < best_bid_price_a:
            buy_stock_id = STOCK_B_ID
            buy_price = best_ask_price_b
            sell_stock_id = STOCK_A_ID
            sell_price = best_bid_price_a
            order_book_buy = order_book_b
            order_book_sell = order_book_a
        else:
            continue

        available_volume_buy = order_book_buy.asks[0].volume
        available_volume_sell = order_book_sell.bids[0].volume

        volume = min(available_volume_buy, available_volume_sell)

        if (not trade_would_breach_position_limit(buy_stock_id, volume, 'bid') and
            not trade_would_breach_position_limit(sell_stock_id, volume, 'ask')):

            print(f"Arbitrage opportunity: BUY {buy_stock_id} at {buy_price}, SELL {sell_stock_id} at {sell_price}")
            exchange.insert_order(instrument_id=sell_stock_id, price=sell_price, volume=volume, side='ask', order_type='ioc')
            exchange.insert_order(instrument_id=buy_stock_id, price=buy_price, volume=volume, side='bid', order_type='ioc')
        else:
            print(f"Skipping trade to avoid breaching position limits.")
