#include "optibook_standalone.h"
#include <iostream>
#include <thread>
#include <chrono>
#include <random>
#include <iomanip>

using namespace OptiBook;

// Mock market data streaming example
class MockMarketDataExample {
private:
    std::mt19937 rng_;
    std::uniform_real_distribution<double> price_change_dist_;
    std::uniform_int_distribution<uint32_t> volume_dist_;
    std::vector<std::string> instruments_;
    std::unordered_map<std::string, double> current_prices_;
    
public:
    MockMarketDataExample() : rng_(std::random_device{}()), 
                             price_change_dist_(-0.02, 0.02),  // +/- 2% moves
                             volume_dist_(1, 100) {
        
        instruments_ = {"AAPL", "GOOGL", "MSFT", "TSLA", "NVDA", "AMZN", "META", "NFLX"};
        
        // Initialize starting prices
        current_prices_["AAPL"] = 150.00;
        current_prices_["GOOGL"] = 2800.00;
        current_prices_["MSFT"] = 300.00;
        current_prices_["TSLA"] = 800.00;
        current_prices_["NVDA"] = 450.00;
        current_prices_["AMZN"] = 3200.00;
        current_prices_["META"] = 250.00;
        current_prices_["NFLX"] = 400.00;
    }
    
    void simulate_market_data_stream() {
        std::cout << "📊 OptiBook C++ Mock Market Data Stream" << std::endl;
        std::cout << "=======================================" << std::endl;
        
        std::cout << "🚀 Streaming market data for " << instruments_.size() << " instruments..." << std::endl;
        std::cout << "   (This simulates live market data feed processing)" << std::endl;
        std::cout << "\nFormat: [INSTRUMENT] BID(vol) ASK(vol) MID SPREAD LAST_TRADE" << std::endl;
        std::cout << std::string(80, '-') << std::endl;
        
        // Simulate 30 seconds of market data
        auto start_time = std::chrono::steady_clock::now();
        auto end_time = start_time + std::chrono::seconds(30);
        
        size_t update_count = 0;
        auto last_stats_time = start_time;
        
        while (std::chrono::steady_clock::now() < end_time) {
            // Pick random instrument to update
            const std::string& instrument = instruments_[rng_() % instruments_.size()];
            
            // Generate price movement
            double& current_price = current_prices_[instrument];
            double price_change = price_change_dist_(rng_) * current_price;
            current_price += price_change;
            
            // Ensure price doesn't go negative
            current_price = std::max(current_price, 1.0);
            
            // Generate order book
            PriceBook book = generate_order_book(instrument, current_price);
            
            // Display market data update
            display_price_update(book);
            
            // Occasionally generate a trade
            if (rng_() % 10 == 0) {
                generate_and_display_trade(book);
            }
            
            update_count++;
            
            // Show performance stats every 5 seconds
            auto now = std::chrono::steady_clock::now();
            if (now - last_stats_time >= std::chrono::seconds(5)) {
                show_performance_stats(update_count, now - start_time);
                last_stats_time = now;
            }
            
            // Control update frequency (simulate high-frequency updates)
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
        }
        
        std::cout << "\n📈 Final Market Summary:" << std::endl;
        std::cout << std::string(50, '=') << std::endl;
        
        for (const auto& [instrument, price] : current_prices_) {
            PriceBook final_book = generate_order_book(instrument, price);
            auto mid = final_book.mid_price();
            auto spread = final_book.spread();
            
            std::cout << std::left << std::setw(8) << instrument << ": ";
            if (mid && spread) {
                std::cout << "Mid=$" << std::fixed << std::setprecision(2) << *mid
                          << " Spread=$" << std::setprecision(4) << *spread
                          << " (" << std::setprecision(1) << Utils::spread_bps(final_book.bids[0].price, final_book.asks[0].price) << " bps)";
            }
            std::cout << std::endl;
        }
        
        std::cout << "\n🎯 Stream Summary:" << std::endl;
        std::cout << "   📊 Total Updates: " << update_count << std::endl;
        std::cout << "   ⚡ Avg Rate: " << std::fixed << std::setprecision(1) 
                  << (update_count / 30.0) << " updates/second" << std::endl;
        std::cout << "   🚀 This demonstrates ultra-fast market data processing!" << std::endl;
    }
    
private:
    PriceBook generate_order_book(const std::string& instrument, double base_price) {
        PriceBook book;
        book.instrument_id = instrument;
        book.timestamp = std::chrono::system_clock::now();
        
        // Generate 5 bid levels
        for (int i = 0; i < 5; ++i) {
            double bid_price = base_price - (i + 1) * 0.01;
            uint32_t bid_volume = volume_dist_(rng_);
            book.bids.emplace_back(bid_price, bid_volume);
        }
        
        // Generate 5 ask levels
        for (int i = 0; i < 5; ++i) {
            double ask_price = base_price + (i + 1) * 0.01;
            uint32_t ask_volume = volume_dist_(rng_);
            book.asks.emplace_back(ask_price, ask_volume);
        }
        
        return book;
    }
    
    void display_price_update(const PriceBook& book) {
        auto mid = book.mid_price();
        auto spread = book.spread();
        
        std::cout << "[" << std::left << std::setw(6) << book.instrument_id << "] ";
        
        if (!book.bids.empty() && !book.asks.empty()) {
            std::cout << std::fixed << std::setprecision(2);
            std::cout << book.bids[0].price << "(" << book.bids[0].volume << ") ";
            std::cout << book.asks[0].price << "(" << book.asks[0].volume << ") ";
            
            if (mid && spread) {
                std::cout << "MID:" << *mid << " SPR:" << std::setprecision(4) << *spread;
            }
        }
        std::cout << std::endl;
    }
    
    void generate_and_display_trade(const PriceBook& book) {
        if (book.bids.empty() || book.asks.empty()) return;
        
        TradeTick trade;
        trade.trade_id = rng_();
        trade.timestamp = std::chrono::system_clock::now();
        trade.instrument_id = book.instrument_id;
        trade.volume = volume_dist_(rng_) / 10 + 1;  // Smaller trade sizes
        trade.buyer = "BUYER_" + std::to_string(rng_() % 100);
        trade.seller = "SELLER_" + std::to_string(rng_() % 100);
        
        // Random trade at bid or ask
        if (rng_() % 2 == 0) {
            trade.price = book.bids[0].price;
            trade.aggressor_side = Side::ASK;  // Seller hitting bid
        } else {
            trade.price = book.asks[0].price;
            trade.aggressor_side = Side::BID;  // Buyer lifting ask
        }
        
        std::cout << "  💰 TRADE: " << trade.instrument_id 
                  << " @$" << std::fixed << std::setprecision(2) << trade.price
                  << " vol:" << trade.volume
                  << " agg:" << side_to_string(trade.aggressor_side) << std::endl;
    }
    
    void show_performance_stats(size_t update_count, std::chrono::steady_clock::duration elapsed) {
        double seconds = std::chrono::duration<double>(elapsed).count();
        double rate = update_count / seconds;
        
        std::cout << "\n⚡ Performance: " << update_count << " updates in " 
                  << std::fixed << std::setprecision(1) << seconds << "s ("
                  << std::setprecision(1) << rate << " updates/sec)" << std::endl;
        std::cout << std::string(80, '-') << std::endl;
    }
};

int main() {
    try {
        MockMarketDataExample example;
        example.simulate_market_data_stream();
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "❌ Error: " << e.what() << std::endl;
        return 1;
    }
}