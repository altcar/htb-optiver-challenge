#include "optibook_standalone.h"
#include <iostream>
#include <thread>
#include <chrono>
#include <random>

using namespace OptiBook;

// Mock trading example that demonstrates the API without network connectivity
class MockTradingExample {
private:
    std::mt19937 rng_;
    std::uniform_real_distribution<double> price_dist_;
    std::uniform_int_distribution<uint32_t> volume_dist_;
    
public:
    MockTradingExample() : rng_(std::random_device{}()), 
                          price_dist_(99.0, 101.0), 
                          volume_dist_(1, 50) {}
    
    void run_mock_trading_session() {
        std::cout << "🚀 OptiBook C++ Mock Trading Session" << std::endl;
        std::cout << "====================================" << std::endl;
        
        // Simulate market data
        PriceBook book;
        book.instrument_id = "OPTIVER_DEMO";
        
        // Generate realistic order book
        for (int i = 0; i < 5; ++i) {
            double bid_price = 100.0 - (i + 1) * 0.01;
            double ask_price = 100.0 + (i + 1) * 0.01;
            uint32_t bid_vol = volume_dist_(rng_);
            uint32_t ask_vol = volume_dist_(rng_);
            
            book.bids.emplace_back(bid_price, bid_vol);
            book.asks.emplace_back(ask_price, ask_vol);
        }
        
        std::cout << "📊 Current Market Data:" << std::endl;
        std::cout << "   Instrument: " << book.instrument_id << std::endl;
        
        auto mid = book.mid_price();
        auto spread = book.spread();
        
        if (mid && spread) {
            std::cout << "   Mid Price: $" << *mid << std::endl;
            std::cout << "   Spread: $" << *spread << std::endl;
            std::cout << "   Spread (bps): " << Utils::spread_bps(book.bids[0].price, book.asks[0].price) << std::endl;
        }
        
        std::cout << "\n📈 Order Book:" << std::endl;
        std::cout << "   BIDS          |   ASKS" << std::endl;
        std::cout << "   Price   Vol   |   Price   Vol" << std::endl;
        
        for (size_t i = 0; i < std::min(book.bids.size(), book.asks.size()); ++i) {
            std::cout << "   " << std::fixed << std::setprecision(2) 
                      << book.bids[i].price << "   " << std::setw(3) << book.bids[i].volume
                      << "   |   " << book.asks[i].price << "   " << std::setw(3) << book.asks[i].volume
                      << std::endl;
        }
        
        // Simulate trading logic
        std::cout << "\n🤖 Executing Market Making Strategy..." << std::endl;
        
        if (mid) {
            double our_bid = *mid - 0.005;  // 0.5 cent spread
            double our_ask = *mid + 0.005;
            uint32_t our_size = 10;
            
            std::cout << "   📤 Placing BID: $" << our_bid << " for " << our_size << " shares" << std::endl;
            std::cout << "   📤 Placing ASK: $" << our_ask << " for " << our_size << " shares" << std::endl;
            
            // Simulate order responses
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            
            std::cout << "   ✅ BID Order ID: 12345 - ACCEPTED" << std::endl;
            std::cout << "   ✅ ASK Order ID: 12346 - ACCEPTED" << std::endl;
        }
        
        // Simulate some trades
        std::cout << "\n💰 Simulating Trades..." << std::endl;
        
        std::vector<Trade> trades;
        for (int i = 0; i < 3; ++i) {
            Trade trade;
            trade.trade_id = 1000 + i;
            trade.instrument_id = book.instrument_id;
            trade.order_id = 12345 + (i % 2);
            trade.price = price_dist_(rng_);
            trade.volume = volume_dist_(rng_) / 5 + 1;  // Smaller trades
            trade.side = (i % 2 == 0) ? Side::BID : Side::ASK;
            trade.timestamp = std::chrono::system_clock::now();
            
            trades.push_back(trade);
            
            std::cout << "   🔄 TRADE: " << trade.instrument_id 
                      << " @$" << std::fixed << std::setprecision(2) << trade.price
                      << " vol:" << trade.volume
                      << " side:" << side_to_string(trade.side) << std::endl;
            
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        
        // Calculate P&L
        std::cout << "\n📊 Position & P&L Analysis:" << std::endl;
        
        int64_t total_position = 0;
        double total_cash_flow = 0.0;
        double vwap = 0.0;
        uint32_t total_volume = 0;
        
        for (const auto& trade : trades) {
            int64_t signed_volume = (trade.side == Side::BID) ? 
                static_cast<int64_t>(trade.volume) : -static_cast<int64_t>(trade.volume);
            
            total_position += signed_volume;
            total_cash_flow -= signed_volume * trade.price;  // Cash impact
            
            vwap += trade.price * trade.volume;
            total_volume += trade.volume;
        }
        
        if (total_volume > 0) {
            vwap /= total_volume;
        }
        
        std::cout << "   📈 Position: " << total_position << " shares" << std::endl;
        std::cout << "   💵 Cash Flow: $" << std::fixed << std::setprecision(2) << total_cash_flow << std::endl;
        std::cout << "   📊 VWAP: $" << std::fixed << std::setprecision(4) << vwap << std::endl;
        
        if (mid && total_position != 0) {
            double unrealized_pnl = Utils::calculate_pnl(total_position, vwap, *mid);
            std::cout << "   💰 Unrealized P&L: $" << std::fixed << std::setprecision(2) << unrealized_pnl << std::endl;
        }
        
        // Risk metrics
        std::cout << "\n⚠️  Risk Metrics:" << std::endl;
        std::cout << "   📊 Position Limit Check: " << (std::abs(total_position) <= 100 ? "✅ OK" : "❌ EXCEEDED") << std::endl;
        std::cout << "   💰 Notional Exposure: $" << std::fixed << std::setprecision(2) 
                  << std::abs(total_position) * (mid ? *mid : 100.0) << std::endl;
        
        std::cout << "\n🎯 Mock Trading Session Complete!" << std::endl;
        std::cout << "   This demonstrates the OptiBook C++ client API" << std::endl;
        std::cout << "   In live trading, connect to actual exchange feeds" << std::endl;
    }
};

int main() {
    try {
        MockTradingExample example;
        example.run_mock_trading_session();
        return 0;
    } catch (const std::exception& e) {
        std::cerr << "❌ Error: " << e.what() << std::endl;
        return 1;
    }
}