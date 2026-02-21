#pragma once

#include "base_client.h"
#include "common_types.h"
#include <functional>
#include <unordered_map>
#include <unordered_set>
#include <deque>
#include <mutex>
#include <shared_mutex>
#include <atomic>

namespace OptiBook {

class ExchangeClient : public BaseClient {
public:
    // Ultra-fast callback types for trading events
    using OrderUpdateCallback = std::function<void(const Order&)>;
    using TradeCallback = std::function<void(const Trade&)>;
    using SingleSidedBookingCallback = std::function<void(const SingleSidedBooking&)>;
    using PositionUpdateCallback = std::function<void(const Positions&)>;
    using NotificationCallback = std::function<void(const std::string& source, const std::string& message)>;

private:
    // Connection state
    std::string username_;
    std::string password_;
    std::string admin_password_;
    bool is_admin_;
    
    // Callbacks for real-time events
    OrderUpdateCallback order_update_callback_;
    TradeCallback trade_callback_;
    SingleSidedBookingCallback single_sided_booking_callback_;
    PositionUpdateCallback position_update_callback_;
    NotificationCallback notification_callback_;
    
    // High-performance data storage
    mutable std::shared_mutex orders_mutex_;
    std::unordered_map<uint64_t, Order> outstanding_orders_;
    std::unordered_map<std::string, std::unordered_set<uint64_t>> orders_by_instrument_;
    
    mutable std::shared_mutex trades_mutex_;
    std::deque<Trade> trade_history_;
    size_t max_trade_history_;
    
    mutable std::shared_mutex positions_mutex_;
    Positions current_positions_;
    
    mutable std::shared_mutex ssb_mutex_;
    std::deque<SingleSidedBooking> ssb_history_;
    size_t max_ssb_history_;
    
    // Fast order ID tracking
    std::atomic<uint64_t> last_order_id_{0};

public:
    ExchangeClient(const std::string& host = "", uint16_t port = 0,
                   const std::string& username = "", const std::string& password = "",
                   const std::string& admin_password = "",
                   size_t max_trade_history = 10000,
                   size_t max_ssb_history = 1000);
    
    ~ExchangeClient() override = default;
    
    bool connect() override;
    bool login();
    bool admin_login();
    
    // Trading operations - ultra-fast order management
    std::future<InsertOrderResponse> insert_order(const std::string& instrument_id, 
                                                  double price, uint32_t volume, 
                                                  Side side, OrderType order_type = OrderType::LIMIT);
    
    std::future<AmendOrderResponse> amend_order(const std::string& instrument_id, 
                                               uint64_t order_id, uint32_t new_volume);
    
    std::future<DeleteOrderResponse> delete_order(const std::string& instrument_id, uint64_t order_id);
    std::future<DeleteOrderResponse> delete_all_orders(const std::string& instrument_id);
    
    // Advanced order management
    std::future<InsertOrderResponse> insert_ioc_order(const std::string& instrument_id, 
                                                      double price, uint32_t volume, Side side);
    
    // Bulk operations for high-frequency trading
    std::vector<std::future<InsertOrderResponse>> insert_orders_bulk(
        const std::vector<std::tuple<std::string, double, uint32_t, Side, OrderType>>& orders);
    
    std::vector<std::future<DeleteOrderResponse>> delete_orders_bulk(
        const std::vector<std::pair<std::string, uint64_t>>& orders);
    
    // Position and order queries - thread-safe
    Positions get_positions() const;
    int64_t get_position(const std::string& instrument_id) const;
    double get_cash() const;
    
    std::vector<Order> get_outstanding_orders() const;
    std::vector<Order> get_outstanding_orders_for_instrument(const std::string& instrument_id) const;
    std::optional<Order> get_order(uint64_t order_id) const;
    
    std::vector<Trade> get_trade_history(size_t max_count = 0) const;
    std::vector<SingleSidedBooking> get_ssb_history(size_t max_count = 0) const;
    
    // Callback setters
    void set_order_update_callback(OrderUpdateCallback callback) { order_update_callback_ = std::move(callback); }
    void set_trade_callback(TradeCallback callback) { trade_callback_ = std::move(callback); }
    void set_single_sided_booking_callback(SingleSidedBookingCallback callback) { single_sided_booking_callback_ = std::move(callback); }
    void set_position_update_callback(PositionUpdateCallback callback) { position_update_callback_ = std::move(callback); }
    void set_notification_callback(NotificationCallback callback) { notification_callback_ = std::move(callback); }
    
    // Advanced trading utilities
    bool has_position(const std::string& instrument_id) const;
    bool is_long(const std::string& instrument_id) const;
    bool is_short(const std::string& instrument_id) const;
    
    // Risk management helpers
    double calculate_notional_value(const std::string& instrument_id, double price) const;
    double get_total_exposure() const;
    
protected:
    void on_connected() override;
    void on_message_received(capnp::MallocMessageBuilder& message);
    void reset_data() override;

private:
    void handle_order_update(const capnp::MallocMessageBuilder& message);
    void handle_trade(const capnp::MallocMessageBuilder& message);
    void handle_single_sided_booking(const capnp::MallocMessageBuilder& message);
    void handle_forced_disconnect(const capnp::MallocMessageBuilder& message);
    void handle_notification(const capnp::MallocMessageBuilder& message);
    
    void update_order_tracking(const Order& order);
    void remove_order_tracking(uint64_t order_id);
};

} // namespace OptiBook