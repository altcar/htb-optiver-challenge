cmake_minimum_required(VERSION 3.16)
project(OptiBookCppClient VERSION 1.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Compiler optimizations for maximum speed
set(CMAKE_CXX_FLAGS_RELEASE "-O3 -DNDEBUG -march=native -mtune=native -flto")
set(CMAKE_BUILD_TYPE Release)

# Find required packages
find_package(PkgConfig REQUIRED)
find_package(Threads REQUIRED)

# Cap'n Proto
pkg_check_modules(CAPNP REQUIRED capnp)
pkg_check_modules(KJ REQUIRED kj)
pkg_check_modules(KJ_ASYNC REQUIRED kj-async)

# Find capnp compiler
find_program(CAPNP_EXECUTABLE capnp REQUIRED)

# Generate Cap'n Proto C++ files
set(CAPNP_SRCS)
file(GLOB CAPNP_FILES "idl/*.capnp")
foreach(capnp_file ${CAPNP_FILES})
    get_filename_component(capnp_name ${capnp_file} NAME_WE)
    set(capnp_output "${CMAKE_CURRENT_BINARY_DIR}/idl/${capnp_name}.capnp.c++")
    set(capnp_header "${CMAKE_CURRENT_BINARY_DIR}/idl/${capnp_name}.capnp.h")
    
    add_custom_command(
        OUTPUT ${capnp_output} ${capnp_header}
        COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/idl
        COMMAND ${CAPNP_EXECUTABLE} compile -oc++:${CMAKE_CURRENT_BINARY_DIR}/idl --src-prefix=${CMAKE_CURRENT_SOURCE_DIR} ${capnp_file}
        DEPENDS ${capnp_file}
        COMMENT "Generating C++ code for ${capnp_file}"
    )
    
    list(APPEND CAPNP_SRCS ${capnp_output})
endforeach()

# Include directories
include_directories(
    ${CMAKE_CURRENT_SOURCE_DIR}/include
    ${CMAKE_CURRENT_BINARY_DIR}/idl
    ${CAPNP_INCLUDE_DIRS}
    ${KJ_INCLUDE_DIRS}
    ${KJ_ASYNC_INCLUDE_DIRS}
)

# Source files
set(SOURCES
    src/base_client.cpp
    src/exchange_client.cpp
    src/info_client.cpp
    src/common_types.cpp
    src/network_client.cpp
    ${CAPNP_SRCS}
)

# Create the library
add_library(optibook_cpp STATIC ${SOURCES})

# Link libraries
target_link_libraries(optibook_cpp 
    ${CAPNP_LIBRARIES}
    ${KJ_LIBRARIES}
    ${KJ_ASYNC_LIBRARIES}
    Threads::Threads
)

# Create example executables
add_executable(trading_example examples/trading_example.cpp)
target_link_libraries(trading_example optibook_cpp)

add_executable(market_data_example examples/market_data_example.cpp)
target_link_libraries(market_data_example optibook_cpp)

# Install targets
install(TARGETS optibook_cpp trading_example market_data_example
    LIBRARY DESTINATION lib
    ARCHIVE DESTINATION lib
    RUNTIME DESTINATION bin
)

install(DIRECTORY include/ DESTINATION include)
install(DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}/idl/ DESTINATION include/idl
    FILES_MATCHING PATTERN "*.h"
)