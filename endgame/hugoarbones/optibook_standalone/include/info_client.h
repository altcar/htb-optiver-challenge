#pragma once

#include "base_client.h"
#include "common_types.h"
#include <functional>
#include <unordered_map>
#include <deque>
#include <mutex>
#include <shared_mutex>

namespace OptiBook {

class InfoClient : public BaseClient {
public:
    // Callback function types for ultra-fast handling
    using PriceBookCallback = std::function<void(const PriceBook&)>;
    using TradeTickCallback = std::function<void(const TradeTick&)>;
    using InstrumentCallback = std::function<void(const Instrument&)>;
    using SocialMediaCallback = std::function<void(const SocialMediaFeed&)>;

private:
    // Callbacks
    PriceBookCallback price_book_callback_;
    TradeTickCallback trade_tick_callback_;
    InstrumentCallback instrument_created_callback_;
    InstrumentCallback instrument_expired_callback_;
    InstrumentCallback instrument_paused_callback_;
    InstrumentCallback instrument_resumed_callback_;
    SocialMediaCallback social_media_callback_;
    
    // Data storage with high-performance access
    mutable std::shared_mutex price_books_mutex_;
    std::unordered_map<std::string, PriceBook> price_books_;
    
    mutable std::shared_mutex instruments_mutex_;
    std::unordered_map<std::string, Instrument> instruments_;
    
    mutable std::shared_mutex trade_history_mutex_;
    std::unordered_map<std::string, std::deque<TradeTick>> trade_history_;
    size_t max_trade_history_;
    
    mutable std::shared_mutex social_media_mutex_;
    std::deque<SocialMediaFeed> social_media_history_;
    size_t max_social_media_history_;
    
    std::string admin_password_;

public:
    InfoClient(const std::string& host = "", uint16_t port = 0, 
               size_t max_trade_history = 10000, 
               size_t max_social_media_history = 1000,
               const std::string& admin_password = "");
    
    ~InfoClient() override = default;
    
    bool connect() override;
    
    // Callback setters for real-time data processing
    void set_price_book_callback(PriceBookCallback callback) { price_book_callback_ = std::move(callback); }
    void set_trade_tick_callback(TradeTickCallback callback) { trade_tick_callback_ = std::move(callback); }
    void set_instrument_created_callback(InstrumentCallback callback) { instrument_created_callback_ = std::move(callback); }
    void set_instrument_expired_callback(InstrumentCallback callback) { instrument_expired_callback_ = std::move(callback); }
    void set_instrument_paused_callback(InstrumentCallback callback) { instrument_paused_callback_ = std::move(callback); }
    void set_instrument_resumed_callback(InstrumentCallback callback) { instrument_resumed_callback_ = std::move(callback); }
    void set_social_media_callback(SocialMediaCallback callback) { social_media_callback_ = std::move(callback); }
    
    // Fast data access methods (thread-safe)
    std::optional<PriceBook> get_price_book(const std::string& instrument_id) const;
    std::unordered_map<std::string, PriceBook> get_all_price_books() const;
    
    std::optional<Instrument> get_instrument(const std::string& instrument_id) const;
    std::vector<Instrument> get_all_instruments() const;
    
    std::vector<TradeTick> get_trade_history(const std::string& instrument_id, size_t max_count = 0) const;
    std::vector<SocialMediaFeed> get_social_media_history(size_t max_count = 0) const;
    
    // Market data analysis helpers
    std::optional<double> get_last_traded_price(const std::string& instrument_id) const;
    std::optional<double> get_mid_price(const std::string& instrument_id) const;
    std::optional<double> get_spread(const std::string& instrument_id) const;
    double get_volume_at_price(const std::string& instrument_id, Side side, double price) const;
    
protected:
    void on_connected() override;
    void on_message_received(capnp::MallocMessageBuilder& message);
    void reset_data() override;

private:
    void handle_price_book_update(const capnp::MallocMessageBuilder& message);
    void handle_trade_tick(const capnp::MallocMessageBuilder& message);
    void handle_instrument_created(const capnp::MallocMessageBuilder& message);
    void handle_instrument_expired(const capnp::MallocMessageBuilder& message);
    void handle_instrument_paused(const capnp::MallocMessageBuilder& message);
    void handle_instrument_resumed(const capnp::MallocMessageBuilder& message);
    void handle_social_media_feed(const capnp::MallocMessageBuilder& message);
    
    void subscribe_to_feeds();
};

} // namespace OptiBook