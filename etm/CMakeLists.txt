cmake_minimum_required(VERSION 3.16)
project(etm_web_decoder LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 11)
set(OPENCSD_BUILD_MIN_LIB_STATIC ON CACHE BOOL "" FORCE)
set(OPENCSD_FULL_PROJECT_LAYOUT OFF CACHE BOOL "" FORCE)
set(BUILD_TESTING OFF CACHE BOOL "" FORCE)
add_subdirectory(vendor/OpenCSD-1.8.3)
add_executable(etm_decode native/decoder.cpp)
target_link_libraries(etm_decode PRIVATE opencsd_c_api)
if(MINGW)
  target_link_options(etm_decode PRIVATE -static -static-libgcc -static-libstdc++)
endif()

# P5b 原生物理前端（KVDAT 分块读取 → 滤波/DDR → TPIU 解复用）。不依赖 OpenCSD：
# 它是 `etm/tools/trace_frontend.py` 参考实现的原生加速版本，语义由该参考实现定义。
# 显式关闭浮点收缩（-ffp-contract=off）：参考实现的取样点算法是“先乘后加”，若编译器
# 合成 FMA，最后一位可能不同，会破坏与参考实现逐字节等价的前提。
add_executable(etm_frontend native/frontend.cpp)
set_property(TARGET etm_frontend PROPERTY CXX_STANDARD 17)
target_compile_options(etm_frontend PRIVATE -ffp-contract=off)
if(MINGW)
  target_link_options(etm_frontend PRIVATE -municode -static -static-libgcc -static-libstdc++)
endif()

# Optional bulk indexer. The Python implementation remains the portable fallback.
if(MINGW)
  get_filename_component(_compiler_bin "${CMAKE_CXX_COMPILER}" DIRECTORY)
  get_filename_component(_compiler_root "${_compiler_bin}" DIRECTORY)
  list(APPEND CMAKE_PREFIX_PATH "${_compiler_root}")
  set(_saved_library_suffixes "${CMAKE_FIND_LIBRARY_SUFFIXES}")
  set(CMAKE_FIND_LIBRARY_SUFFIXES .a)
endif()
find_package(SQLite3 QUIET)
if(MINGW)
  set(CMAKE_FIND_LIBRARY_SUFFIXES "${_saved_library_suffixes}")
endif()
if(SQLite3_FOUND)
  add_executable(etm_index native/indexer.cpp)
  set_property(TARGET etm_index PROPERTY CXX_STANDARD 17)
  target_link_libraries(etm_index PRIVATE SQLite::SQLite3)
  if(MINGW)
    target_link_options(etm_index PRIVATE -municode -static -static-libgcc -static-libstdc++)
  endif()
endif()

# R2 原生 store builder（M0/M1：prescan + native.bin 诊断；不依赖 OpenCSD/SQLite/Python）。
add_executable(etm_storebuild native/storebuild.cpp)
set_property(TARGET etm_storebuild PROPERTY CXX_STANDARD 17)
if(MINGW)
  target_link_libraries(etm_storebuild PRIVATE psapi z crypto)
  target_link_options(etm_storebuild PRIVATE -municode -static -static-libgcc -static-libstdc++)
endif()
execute_process(COMMAND git rev-parse --short=12 HEAD
  WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
  OUTPUT_VARIABLE ETM_STOREBUILD_SHA OUTPUT_STRIP_TRAILING_WHITESPACE ERROR_QUIET)
if(NOT ETM_STOREBUILD_SHA)
  set(ETM_STOREBUILD_SHA "unknown")
endif()
target_compile_definitions(etm_storebuild PRIVATE ETM_STOREBUILD_SHA="${ETM_STOREBUILD_SHA}")
