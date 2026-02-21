#pragma once

#include <string>
#include <vector>
#include <chrono>
#include <optional>
#include <unordered_map>
#include <memory>

namespace OptiBook {

enum class Side {
    BID,
    ASK
};

enum class Action {
    BUY,
    SELL
};

enum class OrderType {
    LIMIT,
    IOC
};

struct PriceVolume {
    double price;
    uint32_t volume;
    
    PriceVolume() = default;
    PriceVolume(double p, uint32_t v) : price(p), volume(v) {}
};

struct PriceBook {
    std::string instrument_id;
    std::vector<PriceVolume> bids;
    std::vector<PriceVolume> asks;
    std::chrono::system_clock::time_point timestamp;
    
    // Fast lookup helpers
    std::optional<double> best_bid() const {
        return bids.empty() ? std::nullopt : std::optional<double>(bids[0].price);
    }
    
    std::optional<double> best_ask() const {
        return asks.empty() ? std::nullopt : std::optional<double>(asks[0].price);
    }
    
    std::optional<double> mid_price() const {
        auto bid = best_bid();
        auto ask = best_ask();
        if (bid && ask) {
            return (*bid + *ask) / 2.0;
        }
        return std::nullopt;
    }
    
    std::optional<double> spread() const {
        auto bid = best_bid();
        auto ask = best_ask();
        if (bid && ask) {
            return *ask - *bid;
        }
        return std::nullopt;
    }
};

struct Order {
    std::string instrument_id;
    uint64_t order_id;
    double price;
    uint32_t volume;
    Side side;
    std::chrono::system_clock::time_point timestamp;
};

struct Trade {
    uint64_t trade_id;
    std::chrono::system_clock::time_point timestamp;
    std::string instrument_id;
    uint64_t order_id;
    double price;
    uint32_t volume;
    Side side;
};

struct TradeTick {
    uint64_t trade_id;
    std::chrono::system_clock::time_point timestamp;
    std::string instrument_id;
    double price;
    uint32_t volume;
    Side aggressor_side;
    std::string buyer;
    std::string seller;
};

struct SingleSidedBooking {
    uint64_t trade_id;
    std::chrono::system_clock::time_point timestamp;
    std::string username;
    std::string instrument_id;
    double price;
    uint32_t volume;
    Action action;
};

struct InstrumentPosition {
    std::string instrument_id;
    int64_t position;
    double cash;
};

struct Positions {
    uint64_t based_on_trade_id;
    std::vector<InstrumentPosition> positions;
    
    // Helper to get position for specific instrument
    int64_t get_position(const std::string& instrument_id) const {
        for (const auto& pos : positions) {
            if (pos.instrument_id == instrument_id) {
                return pos.position;
            }
        }
        return 0;
    }
    
    // Helper to get cash
    double get_cash() const {
        for (const auto& pos : positions) {
            if (pos.instrument_id.empty()) {  // Cash position has empty instrument_id
                return pos.cash;
            }
        }
        return 0.0;
    }
};

struct Instrument {
    std::string instrument_id;
    double tick_size;
    std::string extra_info;  // JSON string
    double absolute_price_change_limit;
    double relative_price_change_limit;
    double last_traded_price;
    bool is_active;
    bool is_paused;
};

struct SocialMediaFeed {
    std::chrono::system_clock::time_point timestamp;
    std::string post;
    std::string meta_data;
};

// Response structures
struct InsertOrderResponse {
    uint64_t order_id;
    bool success;
    std::string error_reason;
};

struct AmendOrderResponse {
    bool success;
    std::string error_reason;
};

struct DeleteOrderResponse {
    bool success;
    std::string error_reason;
};

// Utility functions
std::string side_to_string(Side side);
Side string_to_side(const std::string& side_str);
std::string action_to_string(Action action);
Action string_to_action(const std::string& action_str);
std::string order_type_to_string(OrderType order_type);
OrderType string_to_order_type(const std::string& order_type_str);

// Fast conversion from chrono to timestamp
int64_t to_timestamp(const std::chrono::system_clock::time_point& tp);
std::chrono::system_clock::time_point from_timestamp(int64_t timestamp);

} // namespace OptiBook