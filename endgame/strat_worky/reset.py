from optibook.synchronous_client import Exchange

import time
import logging
logger = logging.getLogger('client')
logger.setLevel('ERROR')

print("Setup was successful.")

# create a connection to the exchange
exchange = Exchange()
_ = exchange.connect()

MIN_SELLING_PRICE = 0.10
MAX_BUYING_PRICE = 100000.00

positions = exchange.get_positions()
pnl = exchange.get_pnl()

print(f'Positions before: {positions}')
print(f'\nPnL before: {pnl:.2f}')

print(f'\nTrading out of positions')
for iid, pos in positions.items():
    if pos > 0:
        print(f'-- Inserting sell order for {pos} lots of {iid}, with limit price {MIN_SELLING_PRICE:.2f}')
        exchange.insert_order(iid, price=MIN_SELLING_PRICE, volume=pos, side='ask', order_type='ioc')
    elif pos < 0:
        print(f'-- Inserting buy order for {abs(pos)} lots of {iid}, with limit price {MAX_BUYING_PRICE:.2f}')
        exchange.insert_order(iid, price=MAX_BUYING_PRICE, volume=-pos, side='bid', order_type='ioc')
    else:
        print(f'-- No initial position in {iid}, skipping..')
    
    time.sleep(0.10)

time.sleep(1.0)

positions = exchange.get_positions()
pnl = exchange.get_pnl()
print(f'\nPositions after: {positions}')
print(f'\nPnL after: {pnl:.2f}')

exchange.disconnect()