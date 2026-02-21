#include "exchange_client.h"
#include "info_client.h"
#include <iostream>
#include <thread>
#include <chrono>

using namespace OptiBook;

int main() {
    std::cout << "OptiBook C++ High-Frequency Trading Example" << std::endl;
    
    try {
        // Create clients
        InfoClient info_client("localhost", 7001);
        ExchangeClient exchange_client("localhost", 7002, "trader1", "password123");
        
        // Set up ultra-fast callbacks for market data
        info_client.set_price_book_callback([&exchange_client](const PriceBook& book) {
            // Lightning-fast market making logic
            auto mid = book.mid_price();
            if (mid && !book.bids.empty() && !book.asks.empty()) {
                double bid_price = *mid - 0.01;
                double ask_price = *mid + 0.01;
                
                // Place aggressive orders on both sides
                exchange_client.insert_order(book.instrument_id, bid_price, 10, Side::BID);
                exchange_client.insert_order(book.instrument_id, ask_price, 10, Side::ASK);
            }
        });
        
        // Set up trade callback for risk management
        exchange_client.set_trade_callback([&exchange_client](const Trade& trade) {
            std::cout << "Trade executed: " << trade.instrument_id 
                      << " @" << trade.price << " vol:" << trade.volume << std::endl;
            
            // Check position limits
            int64_t position = exchange_client.get_position(trade.instrument_id);
            if (std::abs(position) > 100) {
                std::cout << "Position limit exceeded, flattening..." << std::endl;
                // Delete all orders for this instrument
                exchange_client.delete_all_orders(trade.instrument_id);
            }
        });
        
        // Connect to exchange
        if (!info_client.connect()) {
            std::cerr << "Failed to connect to info feed" << std::endl;
            return 1;
        }
        
        if (!exchange_client.connect()) {
            std::cerr << "Failed to connect to exchange" << std::endl;
            return 1;
        }
        
        std::cout << "Connected successfully! Starting high-frequency trading..." << std::endl;
        
        // Run for 30 seconds
        std::this_thread::sleep_for(std::chrono::seconds(30));
        
        // Get final positions
        auto positions = exchange_client.get_positions();
        std::cout << "\nFinal positions:" << std::endl;
        for (const auto& pos : positions.positions) {
            if (!pos.instrument_id.empty()) {
                std::cout << pos.instrument_id << ": " << pos.position 
                          << " (cash impact: " << pos.cash << ")" << std::endl;
            }
        }
        
        std::cout << "Total cash: " << exchange_client.get_cash() << std::endl;
        
        // Clean up - cancel all orders
        auto instruments = info_client.get_all_instruments();
        for (const auto& instrument : instruments) {
            auto orders = exchange_client.get_outstanding_orders_for_instrument(instrument.instrument_id);
            if (!orders.empty()) {
                std::cout << "Cancelling " << orders.size() << " orders for " 
                          << instrument.instrument_id << std::endl;
                exchange_client.delete_all_orders(instrument.instrument_id);
            }
        }
        
    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }
    
    std::cout << "Trading session completed successfully!" << std::endl;
    return 0;
}