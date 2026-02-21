#!/bin/bash

# OptiBook C++ Client Build Script
# Builds the high-performance C++ trading client

set -e

echo "🚀 Building OptiBook C++ High-Performance Client..."

# Check if we're in the right directory
if [ ! -f "CMakeLists.txt" ]; then
    echo "❌ Error: Please run this script from the optibook_cpp directory"
    exit 1
fi

# Create build directory
echo "📁 Creating build directory..."
mkdir -p build
cd build

# Configure with maximum optimizations
echo "⚙️  Configuring CMake with aggressive optimizations..."
cmake .. \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_CXX_FLAGS="-O3 -DNDEBUG -march=native -mtune=native -flto -ffast-math" \
    -DCMAKE_EXE_LINKER_FLAGS="-flto" \
    -DCMAKE_VERBOSE_MAKEFILE=ON

# Build with all CPU cores
echo "🔨 Building with $(nproc) parallel jobs..."
make -j$(nproc)

echo "✅ Build completed successfully!"
echo ""
echo "📦 Built artifacts:"
echo "   📚 Library: liboptibook_cpp.a"
echo "   🤖 Trading Example: trading_example"
echo "   📊 Market Data Example: market_data_example"
echo ""
echo "🚀 Run examples:"
echo "   ./trading_example"
echo "   ./market_data_example"
echo ""
echo "⚡ Performance note: This build is optimized for your specific CPU architecture"
echo "   Use 'make clean && make' to rebuild if deploying to different hardware"