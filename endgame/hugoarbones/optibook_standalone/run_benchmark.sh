#!/bin/bash
cd "$(dirname "$0")"
echo "⚡ Running OptiBook C++ Performance Benchmark..."
./bin/performance_benchmark "$@"
