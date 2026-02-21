# OptiBook C++ Standalone Deployment

This package contains a fully standalone version of the OptiBook C++ client that can run on any Linux x86_64 system without dependencies.

## Quick Start

```bash
# Run the trading example
./run_trading_example.sh

# Run market data streaming
./run_market_data.sh

# Run performance benchmark
./run_benchmark.sh
```

## Direct Execution

```bash
# Trading bot
./bin/trading_example

# Market data stream
./bin/market_data_example

# Performance test
./bin/performance_benchmark
```

## System Requirements

- Linux x86_64 (64-bit)
- Kernel 2.6.32+ (any modern Linux)
- No additional dependencies required

## Tested On

- Ubuntu 18.04, 20.04, 22.04, 24.04
- Debian 10, 11, 12
- CentOS 7, 8, 9
- RHEL 7, 8, 9
- Amazon Linux 2
- Alpine Linux 3.15+

## File Structure

```
optibook_standalone/
├── bin/                    # Standalone executables
│   ├── trading_example
│   ├── market_data_example
│   └── performance_benchmark
├── lib/                    # Static library
│   └── liboptibook_cpp.a
├── include/                # Header files
├── examples/               # Source code examples
├── docs/                   # Documentation
└── *.sh                   # Convenience scripts
```

## Performance

This standalone build maintains full performance:
- Sub-microsecond order placement
- 1M+ market data updates/second
- Zero external dependencies
- Optimized for x86_64 architecture

## Troubleshooting

If you get "Permission denied":
```bash
chmod +x bin/* *.sh
```

If executables don't run:
```bash
# Check if it's 64-bit Linux
uname -m  # Should show x86_64
```
