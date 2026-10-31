# kiko-tqt3 Tests - CMake Build Configuration
# Copyright (c) 2026 Peter Kokosharov <peter.kokosharov@gmail.com>
# LLM: Nemotron Ultra
# Licensed under MIT License

cmake_minimum_required(VERSION 3.10)
project(kiko-tqt3-tests LANGUAGES CXX C)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# TDE/R14 paths - Q4OS installs TQt3 headers in /usr/include/tqt3
set(TQT_INCLUDE_DIRS "/usr/include/tqt3" CACHE PATH "TQt3 include dirs")
set(TQT_COMPAT_INCLUDE_DIRS "${CMAKE_CURRENT_SOURCE_DIR}/../compat_headers" CACHE PATH "TQt3 compat headers")
set(TQT_LIB_DIRS "/usr/lib/x86_64-linux-gnu" CACHE PATH "TQt3 lib dirs")
set(TDE_INCLUDE_DIRS "/opt/trinity/include" CACHE PATH "TDE include dirs")
set(TDE_LIB_DIRS "/opt/trinity/lib" CACHE PATH "TDE lib dirs")

find_path(TQT_QT_H "ntqglobal.h" PATHS ${TQT_INCLUDE_DIRS} NO_DEFAULT_PATH)
find_library(TQT_LIBRARY tqt-mt PATHS ${TQT_LIB_DIRS} NO_DEFAULT_PATH)
find_path(TDE_KAPP_H "kapp.h" PATHS ${TDE_INCLUDE_DIRS} /usr/include NO_DEFAULT_PATH)
find_library(TDE_TDECORE_LIB tdecore PATHS ${TDE_LIB_DIRS} NO_DEFAULT_PATH)
find_library(TDE_TDEUI_LIB tdeui PATHS ${TDE_LIB_DIRS} NO_DEFAULT_PATH)

if(NOT TQT_QT_H OR NOT TQT_LIBRARY)
    message(FATAL_ERROR "TQt development headers/libs not found. Install: libtqt3-mt-dev")
endif()

# GLib for process implementation
find_package(PkgConfig REQUIRED)
pkg_check_modules(GLIB2 REQUIRED glib-2.0)

# Find Qt3/TQt3 moc
find_program(TQT_MOC tqmoc NAMES moc PATHS /opt/trinity/bin /usr/lib/tqt3/bin /usr/bin)

# Process library (standalone, no TDE deps)
add_library(kiko_process STATIC
    ../src/kiko_process.cpp
)

target_include_directories(kiko_process PUBLIC
    ${GLIB2_INCLUDE_DIRS}
    ${CMAKE_CURRENT_SOURCE_DIR}/../src
)

target_link_libraries(kiko_process
    ${GLIB2_LIBRARIES}
)

# Test runner library
add_library(test_runner STATIC
    test_runner.cpp
)

target_include_directories(test_runner PUBLIC
    ${TQT_COMPAT_INCLUDE_DIRS}
    ${TQT_INCLUDE_DIRS}
    ${TDE_INCLUDE_DIRS}
    ${CMAKE_CURRENT_SOURCE_DIR}
)

target_link_libraries(test_runner
    ${TQT_LIBRARY}
    ${TDE_TDECORE_LIB}
    ${TDE_TDEUI_LIB}
)

# Main test executable with main()
add_executable(run_tests
    test_main.cpp
    test_state_machine.cpp
    ../src/kiko_process.cpp
    test_runner.cpp
)

# Test executable for KikoLogPanel
add_executable(test_log_panel
    test_log_panel.cpp
    ../src/kiko_log_panel.cpp
)

target_include_directories(test_log_panel PRIVATE
    ${TQT_COMPAT_INCLUDE_DIRS}
    ${TQT_INCLUDE_DIRS}
    ${TDE_INCLUDE_DIRS}
    ${GLIB2_INCLUDE_DIRS}
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../src
)

target_link_libraries(test_log_panel
    ${TQT_LIBRARY}
    ${TDE_TDECORE_LIB}
    ${TDE_TDEUI_LIB}
    ${SQLITE3_LIB}
)

target_include_directories(run_tests PRIVATE
    ${TQT_COMPAT_INCLUDE_DIRS}
    ${TQT_INCLUDE_DIRS}
    ${TDE_INCLUDE_DIRS}
    ${GLIB2_INCLUDE_DIRS}
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/../src
)

target_link_libraries(run_tests
    ${TQT_LIBRARY}
    ${TDE_TDECORE_LIB}
    ${TDE_TDEUI_LIB}
    ${GLIB2_LIBRARIES}
)

# MOC for test runner if needed
if(TQT_MOC)
    # test_runner.h doesn't have Q_OBJECT, so no MOC needed
endif()

# Test target
add_custom_target(check
    COMMAND ./run_tests
    DEPENDS run_tests
    WORKING_DIRECTORY ${CMAKE_CURRENT_BINARY_DIR}
    COMMENT "Running kiko-tqt3 tests"
)