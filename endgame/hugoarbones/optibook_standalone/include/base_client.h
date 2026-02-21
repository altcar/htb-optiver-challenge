#pragma once

#include <memory>
#include <string>
#include <functional>
#include <future>
#include <atomic>
#include <queue>
#include <mutex>
#include <condition_variable>
#include <thread>
#include <unordered_map>

#include <kj/async.h>
#include <kj/async-io.h>
#include <capnp/message.h>
#include <capnp/serialize.h>

#include "common_types.h"

namespace OptiBook {

class NetworkClient {
private:
    std::string host_;
    uint16_t port_;
    std::unique_ptr<kj::AsyncIoProvider> io_provider_;
    std::unique_ptr<kj::AsyncIoStream> stream_;
    std::unique_ptr<kj::EventLoop> event_loop_;
    std::atomic<bool> connected_{false};
    std::atomic<bool> should_stop_{false};
    
    // Message handling
    std::queue<capnp::MallocMessageBuilder> outgoing_messages_;
    std::mutex outgoing_mutex_;
    std::condition_variable outgoing_cv_;
    
    std::thread network_thread_;
    std::thread event_loop_thread_;
    
    // Request tracking
    std::atomic<uint32_t> next_request_id_{1};
    std::unordered_map<uint32_t, std::promise<capnp::MallocMessageBuilder>> pending_requests_;
    std::mutex requests_mutex_;

protected:
    virtual void on_connected() {}
    virtual void on_disconnected() {}
    virtual void on_message_received(capnp::MallocMessageBuilder& message) {}

public:
    NetworkClient(const std::string& host, uint16_t port);
    virtual ~NetworkClient();
    
    // Connection management
    bool connect();
    void disconnect();
    bool is_connected() const { return connected_.load(); }
    
    // Message sending
    void send_message(capnp::MallocMessageBuilder&& message);
    std::future<capnp::MallocMessageBuilder> send_request(capnp::MallocMessageBuilder&& message, uint32_t request_id);
    
    // Request ID generation
    uint32_t new_request_id() { return next_request_id_.fetch_add(1); }

private:
    void run_network_loop();
    void run_event_loop();
    void process_incoming_messages();
    void process_outgoing_messages();
};

class BaseClient {
protected:
    std::unique_ptr<NetworkClient> network_client_;
    std::string host_;
    uint16_t port_;
    bool has_connected_{false};

public:
    BaseClient(const std::string& host, uint16_t port);
    virtual ~BaseClient() = default;
    
    virtual bool connect();
    virtual void disconnect();
    bool is_connected() const;
    
protected:
    virtual void on_connected() {}
    virtual void on_disconnected() {}
    virtual void reset_data() {}
};

} // namespace OptiBook