"""
Example script showing how to use both trade and news feed functionality.
"""

from clas_trade import OptiBookTradeStream

def handle_trade(trade):
    print(f"[TRADE] {trade['timestamp']} - {trade['instrument']}: "
          f"{trade['buyer']} -> {trade['seller']} | "
          f"{trade['volume']} @ {trade['price']}")

def handle_news(news):
    print(f"[NEWS] {news['timestamp']} - {news['author']}: {news['post']}")

def handle_connected():
    print("✓ Connected to OptiBook stream")

def handle_disconnected():
    print("✗ Disconnected from OptiBook stream")

def handle_error(error):
    print(f"✗ Error: {error}")

def main():
    # Create stream
    stream = OptiBookTradeStream("htb.optibook.net", "your-username")
    
    # Set callbacks
    stream.on_trade = handle_trade
    stream.on_news = handle_news
    stream.on_connected = handle_connected
    stream.on_disconnected = handle_disconnected
    stream.on_error = handle_error
    
    # Enable storage
    stream.store_trades = True
    stream.news_handler.store_news = True
    
    print("Starting OptiBook stream...")
    
    try:
        # Start in blocking mode
        stream.start(blocking=True)
    except KeyboardInterrupt:
        print("\nStopping stream...")
        stream.stop()
        
        # Save collected data
        stream.news_handler.save_news_to_file("collected_news.json")
        print("Data saved to collected_news.json")

if __name__ == "__main__":
    main()