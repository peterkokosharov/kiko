# SPDX-License-Identifier: MIT
# Author: Peter Kokosharov <peter.kokosharov@gmail.com>
#
# kiko-tray native build.  One file, platform picked automatically:
#   Windows (MSVC + Windows SDK, or MinGW)  -> native/win32  (pure Win32 API)
#   macOS -> native/macos (Cocoa); Linux: -DKT_GUI=gtk (default, GTK3 + XApp) or -DKT_GUI=qt (Qt6/Qt5 Widgets)
# Options:
#   -DKT_BUILD_GUI=OFF     build only the portable core and its tests (no GUI toolkit needed)
#   -DKT_BUILD_TESTS=OFF   skip the tests
#   -DKT_FETCH_CJSON=OFF   do not download cJSON when third_party/cjson is empty
cmake_minimum_required(VERSION 3.16)
project(kiko_tray VERSION 0.1.0 LANGUAGES C)

option(KT_BUILD_GUI "Build the GUI layer for this platform" ON)
option(KT_BUILD_TESTS "Build the CTest tests" ON)
option(KT_FETCH_CJSON "Download cJSON v1.7.18 with FetchContent when third_party/cjson has no sources" ON)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS ON)   # gnu11: clock_gettime/pthread_condattr_setclock need the POSIX feature macros

if(WIN32)
  enable_language(RC)
  add_compile_definitions(UNICODE _UNICODE WIN32_LEAN_AND_MEAN _WIN32_WINNT=0x0601 WINVER=0x0601 NOMINMAX)
  if(MSVC)
    add_compile_definitions(_CRT_SECURE_NO_WARNINGS)
    add_compile_options(/utf-8)   # UTF-8 source and execution charset on MSVC (non-ASCII literals in tests)
  endif()
endif()

function(kt_warnings target)
  if(MSVC)
    target_compile_options(${target} PRIVATE /W4)
  else()
    target_compile_options(${target} PRIVATE -Wall -Wextra)
  endif()
endfunction()

# ---- cJSON: vendored copy first, FetchContent as the fallback ----------------------------------
set(KT_CJSON_DIR "${CMAKE_CURRENT_SOURCE_DIR}/third_party/cjson")
if(EXISTS "${KT_CJSON_DIR}/cJSON.c" AND EXISTS "${KT_CJSON_DIR}/cJSON.h")
  set(KT_CJSON_SRC_DIR "${KT_CJSON_DIR}")
  message(STATUS "cJSON: using the vendored copy in ${KT_CJSON_DIR}")
elseif(KT_FETCH_CJSON)
  include(FetchContent)
  if(POLICY CMP0169)
    cmake_policy(SET CMP0169 OLD)   # we only want the sources, not cJSON's own CMake project
  endif()
  FetchContent_Declare(cjson_src
    GIT_REPOSITORY https://github.com/DaveGamble/cJSON.git
    GIT_TAG        v1.7.18)
  FetchContent_GetProperties(cjson_src)
  if(NOT cjson_src_POPULATED)
    FetchContent_Populate(cjson_src)
  endif()
  set(KT_CJSON_SRC_DIR "${cjson_src_SOURCE_DIR}")
  message(STATUS "cJSON: downloaded v1.7.18 to ${KT_CJSON_SRC_DIR}")
else()
  message(FATAL_ERROR "cJSON not found. Put cJSON.c and cJSON.h into native/third_party/cjson "
                      "(see its README.md) or configure with -DKT_FETCH_CJSON=ON.")
endif()

add_library(kt_cjson STATIC "${KT_CJSON_SRC_DIR}/cJSON.c")   # third-party code: no -Wall on purpose
target_include_directories(kt_cjson PUBLIC "${KT_CJSON_SRC_DIR}")
if(NOT WIN32)
  target_link_libraries(kt_cjson PUBLIC m)
endif()

# ---- portable core ---------------------------------------------------------------------------
find_package(Threads REQUIRED)
add_library(kt_common STATIC
  common/kt_util.c common/kt_json.c common/kt_http.c common/kt_mask.c common/kt_thread.c
  common/kt_dialog.c common/kt_icon.c common/kt_presenter.c)
target_include_directories(kt_common PUBLIC common)
target_compile_definitions(kt_common PUBLIC KT_VERSION="${PROJECT_VERSION}")
target_link_libraries(kt_common PUBLIC kt_cjson Threads::Threads)
if(WIN32)
  target_link_libraries(kt_common PUBLIC ws2_32)
else()
  target_link_libraries(kt_common PUBLIC m)
endif()
kt_warnings(kt_common)

# ---- GUI layer, chosen by platform -------------------------------------------------------------
#   Windows -> win32 | macOS -> macos | Linux/BSD -> KT_GUI = gtk (default) or qt
set(KT_GUI "gtk" CACHE STRING "Linux GUI layer: gtk or qt")
set_property(CACHE KT_GUI PROPERTY STRINGS gtk qt)

if(KT_BUILD_GUI)
  if(WIN32)
    add_executable(kiko-tray WIN32
      win32/win_main.c win32/win_view.c win32/win_dialogs.c win32/win_host.c win32/kiko-tray.rc)
    target_include_directories(kiko-tray PRIVATE win32)
    target_link_libraries(kiko-tray PRIVATE kt_common user32 gdi32 shell32 comctl32 advapi32 ole32)
    kt_warnings(kiko-tray)
    if(MSVC)
      # The manifest (per-monitor DPI, Common Controls 6) is embedded by the .rc file.
      target_link_options(kiko-tray PRIVATE /MANIFEST:NO)
    endif()

  elseif(APPLE)
    # UNVERIFIED: written without a Mac.
    enable_language(OBJC)
    set(CMAKE_OBJC_FLAGS "${CMAKE_OBJC_FLAGS} -fobjc-arc")
    find_library(KT_COCOA Cocoa)
    if(NOT KT_COCOA)
      message(FATAL_ERROR "Cocoa framework not found (install the Xcode Command Line Tools)")
    endif()
    add_executable(kiko-tray MACOSX_BUNDLE macos/mac_main.m macos/mac_view.m macos/mac_host.m)
    target_include_directories(kiko-tray PRIVATE macos)
    target_link_libraries(kiko-tray PRIVATE kt_common ${KT_COCOA})
    set_target_properties(kiko-tray PROPERTIES
      MACOSX_BUNDLE_INFO_PLIST "${CMAKE_CURRENT_SOURCE_DIR}/macos/Info.plist.in"   # LSUIElement: no Dock icon
      MACOSX_BUNDLE_BUNDLE_NAME "kiko-tray"
      MACOSX_BUNDLE_GUI_IDENTIFIER "com.kiko.tray"
      MACOSX_BUNDLE_BUNDLE_VERSION "${PROJECT_VERSION}"
      MACOSX_BUNDLE_SHORT_VERSION_STRING "${PROJECT_VERSION}")

  elseif(UNIX AND KT_GUI STREQUAL "gtk")
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(GTK3 REQUIRED gtk+-3.0)
    pkg_check_modules(XAPP xapp)   # optional: preinstalled on Linux Mint (libxapp-dev to build)
    add_executable(kiko-tray gtk/gtk_main.c gtk/gtk_view.c gtk/gtk_host.c)
    target_include_directories(kiko-tray PRIVATE gtk ${GTK3_INCLUDE_DIRS})
    target_link_directories(kiko-tray PRIVATE ${GTK3_LIBRARY_DIRS})
    target_link_libraries(kiko-tray PRIVATE kt_common ${GTK3_LIBRARIES})
    target_compile_options(kiko-tray PRIVATE ${GTK3_CFLAGS_OTHER} -Wno-deprecated-declarations)
    if(XAPP_FOUND)
      message(STATUS "XApp status icon: found (${XAPP_VERSION}); GtkStatusIcon is the fallback")
      target_compile_definitions(kiko-tray PRIVATE KT_HAVE_XAPP=1)
      target_include_directories(kiko-tray PRIVATE ${XAPP_INCLUDE_DIRS})
      target_link_directories(kiko-tray PRIVATE ${XAPP_LIBRARY_DIRS})
      target_link_libraries(kiko-tray PRIVATE ${XAPP_LIBRARIES})
    else()
      message(STATUS "XApp not found: building with GtkStatusIcon only")
    endif()
    kt_warnings(kiko-tray)
    install(TARGETS kiko-tray RUNTIME DESTINATION bin)

  elseif(UNIX AND KT_GUI STREQUAL "qt")
    # UNVERIFIED: written without a compiler.  Qt6 first, Qt5 as the fallback.
    enable_language(CXX)
    set(CMAKE_CXX_STANDARD 17)
    set(CMAKE_CXX_STANDARD_REQUIRED ON)
    find_package(Qt6 COMPONENTS Widgets QUIET)
    if(Qt6_FOUND)
      set(KT_QT_WIDGETS Qt6::Widgets)
      message(STATUS "Qt: using Qt6")
    else()
      find_package(Qt5 5.15 COMPONENTS Widgets REQUIRED)
      set(KT_QT_WIDGETS Qt5::Widgets)
      message(STATUS "Qt: Qt6 not found, using Qt5")
    endif()
    add_executable(kiko-tray qt/qt_main.cpp qt/qt_view.cpp qt/qt_host.cpp)
    target_include_directories(kiko-tray PRIVATE qt)
    target_link_libraries(kiko-tray PRIVATE kt_common ${KT_QT_WIDGETS})
    if(NOT MSVC)
      target_compile_options(kiko-tray PRIVATE -Wall -Wextra)
    endif()
    install(TARGETS kiko-tray RUNTIME DESTINATION bin)

  else()
    message(FATAL_ERROR "No GUI layer for this platform/KT_GUI. Use -DKT_BUILD_GUI=OFF to build the core only.")
  endif()
endif()

# ---- tests ---------------------------------------------------------------------------------------
if(KT_BUILD_TESTS)
  enable_testing()
  foreach(t test_json test_http test_mask test_util_icon test_presenter test_socket)
    add_executable(${t} tests/${t}.c)
    target_include_directories(${t} PRIVATE tests)
    target_link_libraries(${t} PRIVATE kt_common)
    kt_warnings(${t})
    add_test(NAME ${t} COMMAND ${t})
    set_tests_properties(${t} PROPERTIES TIMEOUT 60)
  endforeach()
endif()
