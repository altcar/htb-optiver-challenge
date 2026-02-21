"""
Dual Product Arbitrage Strategy for Optibook
Monitors 5 pairs of dual products and executes arbitrage trades
"""

from optibook.synchronous_client import Exchange
import time
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Initialize exchange connection
exchange = Exchange()
exchange.connect()

# Define the dual product pairs
# Format: (Product A, Product B, theoretical_relationship)
DUAL_PAIRS = [
    ('NVDA', 'NVDA_DUAL'),  # Replace with actual instrument IDs
    ('CSCO', 'CSCO_DUAL'),
    ('SAN', 'SAN_DUAL'),
    ('ING', 'ING_DUAL'),
    ('PFE', 'PFE_DUAL'),
]
# Strategy parameters
MIN_ARBITRAGE_PROFIT = 0  # Minimum profit per lot to execute arbitrage
MAX_POSITION_SIZE = 100       # Maximum position in any single instrument
TRADE_VOLUME = 10            # Standard trade size
CHECK_INTERVAL = 1/25         # Seconds between checks


def get_best_bid_ask(instrument_id):
    """Get best bid and ask prices and volumes"""
    book = exchange.get_last_price_book(instrument_id)
    
    if not book:
        return None, None, None, None
    
    best_bid = book.bids[0].price if book.bids else None
    best_bid_vol = book.bids[0].volume if book.bids else 0
    best_ask = book.asks[0].price if book.asks else None
    best_ask_vol = book.asks[0].volume if book.asks else 0
    
    return best_bid, best_bid_vol, best_ask, best_ask_vol


def check_arbitrage_opportunity(product_a, product_b):
    """
    Check for arbitrage opportunities between two dual products
    Returns: (has_opportunity, direction, expected_profit, volume)
    direction: 'buy_a_sell_b' or 'buy_b_sell_a'
    """
    
    # Get order book data
    # print(product_a, product_b)
    a_bid, a_bid_vol, a_ask, a_ask_vol = get_best_bid_ask(product_a)
    b_bid, b_bid_vol, b_ask, b_ask_vol = get_best_bid_ask(product_b)

    

    # print(f"a bid: {a_bid}, b ask: {b_ask}")
    # print(f"a ask: {a_ask}, b bid: {b_bid}")
    # print(a_bid_vol, a_ask_vol, b_bid_vol, b_ask_vol)
    
    # Check if we have valid prices
    if None in [a_bid, a_ask, b_bid, b_ask]:
        print("wtf")
        return False, None, 0, 0
    
    # Calculate potential arbitrage profits
    # Opportunity 1: Buy A, Sell B
    profit_buy_a_sell_b = b_bid - a_ask
    volume_1 = min(a_ask_vol, b_bid_vol)

    # print(f"profit_buy_a_sell_b: {profit_buy_a_sell_b}")
    
    # Opportunity 2: Buy B, Sell A
    profit_buy_b_sell_a = a_bid - b_ask
    volume_2 = min(b_ask_vol, a_bid_vol)
    # print(f"profit_buy_b_sell_a: {profit_buy_b_sell_a}")
    # print()

    
    # Check if either opportunity is profitable
    if profit_buy_a_sell_b > MIN_ARBITRAGE_PROFIT and volume_1 > 0:
        return True, 'buy_a_sell_b', profit_buy_a_sell_b, volume_1
    elif profit_buy_b_sell_a > MIN_ARBITRAGE_PROFIT and volume_2 > 0:
        return True, 'buy_b_sell_a', profit_buy_b_sell_a, volume_2
    
    return False, None, 0, 0


def execute_arbitrage(product_a, product_b, direction, volume):
    """Execute the arbitrage trade"""
    
    try:
        if direction == 'buy_a_sell_b':
            # Buy product A
            a_book = exchange.get_last_price_book(product_a)
            buy_price = a_book.asks[0].price if a_book and a_book.asks else None
            
            # Sell product B
            b_book = exchange.get_last_price_book(product_b)
            sell_price = b_book.bids[0].price if b_book and b_book.bids else None
            
            if buy_price and sell_price:
                # Execute with IOC orders for immediate execution
                exchange.insert_order(product_a, price=buy_price, volume=volume, 
                                    side='bid', order_type='ioc')
                exchange.insert_order(product_b, price=sell_price, volume=volume, 
                                    side='ask', order_type='ioc')
                
                logging.info(f"ARBITRAGE: Bought {volume} {product_a} @ {buy_price}, "
                           f"Sold {volume} {product_b} @ {sell_price}")
                return True
                
        elif direction == 'buy_b_sell_a':
            # Buy product B
            b_book = exchange.get_last_price_book(product_b)
            buy_price = b_book.asks[0].price if b_book and b_book.asks else None
            
            # Sell product A
            a_book = exchange.get_last_price_book(product_a)
            sell_price = a_book.bids[0].price if a_book and a_book.bids else None
            
            if buy_price and sell_price:
                # Execute with IOC orders
                exchange.insert_order(product_b, price=buy_price, volume=volume, 
                                    side='bid', order_type='ioc')
                exchange.insert_order(product_a, price=sell_price, volume=volume, 
                                    side='ask', order_type='ioc')
                
                logging.info(f"ARBITRAGE: Bought {volume} {product_b} @ {buy_price}, "
                           f"Sold {volume} {product_a} @ {sell_price}")
                return True
        
    except Exception as e:
        logging.error(f"Error executing arbitrage: {e}")
        return False
    
    return False


def check_positions():
    """Monitor current positions"""
    positions = exchange.get_positions()
    
    for instrument_id, position in positions.items():
        if abs(position) > MAX_POSITION_SIZE:
            logging.warning(f"Position limit exceeded for {instrument_id}: {position}")
    
    return positions


def unwind_positions():
    """Aggressively unwind any open positions"""
    positions = exchange.get_positions()
    
    for instrument_id, position in positions.items():
        if position != 0:
            try:
                book = exchange.get_last_price_book(instrument_id)
                
                if position > 0:  # Long position - need to sell
                    if book and book.bids:
                        price = book.bids[0].price
                        exchange.insert_order(instrument_id, price=price, volume=abs(position),
                                            side='ask', order_type='ioc')
                        logging.info(f"Unwinding {position} lots of {instrument_id} @ {price}")
                        
                elif position < 0:  # Short position - need to buy
                    if book and book.asks:
                        price = book.asks[0].price
                        exchange.insert_order(instrument_id, price=price, volume=abs(position),
                                            side='bid', order_type='ioc')
                        logging.info(f"Unwinding {position} lots of {instrument_id} @ {price}")
                        
            except Exception as e:
                logging.error(f"Error unwinding position for {instrument_id}: {e}")


def main():
    """Main arbitrage loop"""
    
    logging.info("Starting Dual Product Arbitrage Strategy")
    logging.info(f"Monitoring {len(DUAL_PAIRS)} dual pairs")
    
    
    try:
        while True:
            
            # Check each dual pair for arbitrage opportunities
            for product_a, product_b in DUAL_PAIRS:
                has_opp, direction, profit, volume = check_arbitrage_opportunity(
                    product_a, product_b
                )
                
                if has_opp:
                    logging.info(f"Opportunity found: {product_a}/{product_b}, "
                               f"Direction: {direction}, Profit: {profit:.4f}, Volume: {volume}")
                    
                    # Check position limits before trading
                    positions = exchange.get_positions()
                    a_pos = positions.get(product_a, 0)
                    b_pos = positions.get(product_b, 0)
                    
                    # Calculate how much we can actually trade based on position limits
                    if direction == 'buy_a_sell_b':
                        # Buying A: can go from a_pos up to +MAX_POSITION_SIZE
                        max_a_buy = MAX_POSITION_SIZE - a_pos
                        # Selling B: can go from b_pos down to -MAX_POSITION_SIZE
                        max_b_sell = MAX_POSITION_SIZE + b_pos
                        max_trade_volume = min(volume, max_a_buy, max_b_sell)
                        print(max_trade_volume)
                    else:  # buy_b_sell_a
                        # Buying B: can go from b_pos up to +MAX_POSITION_SIZE
                        max_b_buy = MAX_POSITION_SIZE - b_pos
                        # Selling A: can go from a_pos down to -MAX_POSITION_SIZE
                        max_a_sell = MAX_POSITION_SIZE + a_pos
                        max_trade_volume = min(volume, max_b_buy, max_a_sell)
                        print(max_trade_volume)
                    
                    if max_trade_volume > 0:
                        execute_arbitrage(product_a, product_b, direction, max_trade_volume)
                    else:
                        logging.warning(f"Skipping trade due to position limits: a_pos={a_pos}, b_pos={b_pos}")
            
            # Periodic position check and PnL reportin
            
            time.sleep(CHECK_INTERVAL)
            
    except KeyboardInterrupt:
        logging.info("Shutting down strategy...")
        unwind_positions()
        logging.info("Final PnL: {:.2f}".format(exchange.get_pnl()))
        

if __name__ == "__main__":
    main()