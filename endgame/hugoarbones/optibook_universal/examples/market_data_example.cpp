#include "info_client.h"
#include <iostream>
#include <thread>
#include <chrono>
#include <iomanip>

using namespace OptiBook;

int main() {
    std::cout << "OptiBook C++ Market Data Stream Example" << std::endl;
    
    try {
        InfoClient info_client("localhost", 7001);
        
        // Set up ultra-fast market data callbacks
        info_client.set_price_book_callback([](const PriceBook& book) {
            std::cout << std::fixed << std::setprecision(2);
            std::cout << "[" << book.instrument_id << "] ";
            
            if (!book.bids.empty() && !book.asks.empty()) {
                std::cout << "BID: " << book.bids[0].price << "(" << book.bids[0].volume << ") ";
                std::cout << "ASK: " << book.asks[0].price << "(" << book.asks[0].volume << ") ";
                
                auto mid = book.mid_price();
                auto spread = book.spread();
                if (mid && spread) {
                    std::cout << "MID: " << *mid << " SPREAD: " << *spread;
                }
            }
            std::cout << std::endl;
        });
        
        info_client.set_trade_tick_callback([](const TradeTick& trade) {
            std::cout << "[TRADE] " << trade.instrument_id 
                      << " @" << trade.price << " vol:" << trade.volume
                      << " aggressor:" << side_to_string(trade.aggressor_side)
                      << " buyer:" << trade.buyer << " seller:" << trade.seller
                      << std::endl;
        });
        
        info_client.set_instrument_created_callback([](const Instrument& instrument) {
            std::cout << "[NEW INSTRUMENT] " << instrument.instrument_id
                      << " tick_size:" << instrument.tick_size
                      << " active:" << (instrument.is_active ? "YES" : "NO")
                      << std::endl;
        });
        
        info_client.set_social_media_callback([](const SocialMediaFeed& feed) {
            std::cout << "[SOCIAL] " << feed.post << std::endl;
        });
        
        // Connect to market data feed
        if (!info_client.connect()) {
            std::cerr << "Failed to connect to market data feed" << std::endl;
            return 1;
        }
        
        std::cout << "Connected to market data feed! Streaming prices..." << std::endl;
        
        // Stream data for 60 seconds
        std::this_thread::sleep_for(std::chrono::seconds(60));
        
        // Print summary statistics
        std::cout << "\n=== Market Summary ===" << std::endl;
        
        auto price_books = info_client.get_all_price_books();
        for (const auto& [instrument_id, book] : price_books) {
            std::cout << instrument_id << ": ";
            auto mid = book.mid_price();
            auto spread = book.spread();
            if (mid && spread) {
                std::cout << "Mid=" << *mid << " Spread=" << *spread;
                
                auto last_price = info_client.get_last_traded_price(instrument_id);
                if (last_price) {
                    std::cout << " Last=" << *last_price;
                }
            }
            std::cout << std::endl;
        }
        
        // Show trade counts
        auto instruments = info_client.get_all_instruments();
        for (const auto& instrument : instruments) {
            auto trades = info_client.get_trade_history(instrument.instrument_id);
            if (!trades.empty()) {
                std::cout << instrument.instrument_id << ": " << trades.size() 
                          << " trades, last @" << trades.back().price << std::endl;
            }
        }
        
    } catch (const std::exception& e) {
        std::cerr << "Error: " << e.what() << std::endl;
        return 1;
    }
    
    std::cout << "Market data session completed!" << std::endl;
    return 0;
}