#ifndef OPTIBOOK_STANDALONE_H
#define OPTIBOOK_STANDALONE_H

/**
 * OptiBook C++ High-Performance Trading Client - Standalone Version
 * 
 * Ultra-low latency trading client for OptiBook exchange
 * This standalone version includes only the core types and utilities
 * for demonstration purposes without network connectivity.
 */

#include "common_types.h"
#include <iostream>
#include <iomanip>

// Convenience namespace alias
namespace OB = OptiBook;

// Performance hints
#ifdef __GNUC__
#define OPTIBOOK_HOT __attribute__((hot))
#define OPTIBOOK_COLD __attribute__((cold))
#define OPTIBOOK_LIKELY(x) __builtin_expect(!!(x), 1)
#define OPTIBOOK_UNLIKELY(x) __builtin_expect(!!(x), 0)
#else
#define OPTIBOOK_HOT
#define OPTIBOOK_COLD
#define OPTIBOOK_LIKELY(x) (x)
#define OPTIBOOK_UNLIKELY(x) (x)
#endif

// Fast compile-time constants
namespace OptiBook {
namespace Constants {
    constexpr double EPSILON = 1e-9;
    constexpr size_t DEFAULT_TRADE_HISTORY = 10000;
    constexpr size_t DEFAULT_ORDER_BOOK_DEPTH = 50;
    constexpr uint16_t DEFAULT_INFO_PORT = 7001;
    constexpr uint16_t DEFAULT_EXEC_PORT = 7002;
}
}

// Utility functions for common trading operations
namespace OptiBook {
namespace Utils {
    
    // Fast price comparison with epsilon
    inline bool price_equal(double a, double b) {
        return std::abs(a - b) < Constants::EPSILON;
    }
    
    // Calculate spread basis points
    inline double spread_bps(double bid, double ask) {
        if (bid <= 0 || ask <= 0) return 0.0;
        return ((ask - bid) / ((bid + ask) / 2.0)) * 10000.0;
    }
    
    // Calculate position PnL
    inline double calculate_pnl(int64_t position, double entry_price, double current_price) {
        return static_cast<double>(position) * (current_price - entry_price);
    }
    
    // Risk-adjusted position size
    inline uint32_t risk_adjusted_size(double risk_per_trade, double price, double stop_distance) {
        if (stop_distance <= 0 || price <= 0) return 0;
        return static_cast<uint32_t>(risk_per_trade / (stop_distance * price));
    }
    
    // Kelly criterion position sizing
    inline double kelly_position_size(double win_prob, double avg_win, double avg_loss) {
        if (avg_loss <= 0) return 0.0;
        return (win_prob * avg_win - (1.0 - win_prob) * avg_loss) / avg_win;
    }
}
}

// Pre-defined trading strategies namespace
namespace OptiBook {
namespace Strategies {
    
    // Simple market making parameters
    struct MarketMakingParams {
        double spread_multiplier = 1.0;
        uint32_t default_size = 10;
        double max_position = 100.0;
        double tick_size = 0.01;
    };
    
    // Mean reversion parameters  
    struct MeanReversionParams {
        double lookback_period = 20.0;
        double entry_threshold = 2.0;  // Standard deviations
        double exit_threshold = 0.5;
        uint32_t position_size = 10;
    };
    
    // Momentum parameters
    struct MomentumParams {
        double short_period = 5.0;
        double long_period = 20.0;
        double entry_threshold = 0.02;  // 2% move
        uint32_t position_size = 10;
    };
}
}

#endif // OPTIBOOK_STANDALONE_H