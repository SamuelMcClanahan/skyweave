cmake_minimum_required(VERSION 3.16)
project(skyweave_edge C)

# C99, and warnings are errors. The Luckfox SDK ships gcc 8.3.0, so nothing
# here may need a newer standard: a daemon that only builds on the
# developer's clang is not a daemon.
set(CMAKE_C_STANDARD 99)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(CMAKE_C_EXTENSIONS OFF)

if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# Strict C99 hides every POSIX declaration behind a feature-test macro, and
# uclibc is stricter about it than macOS libc — `clock_gettime` and
# `struct timespec` simply vanish. Declared here rather than relaxed to
# -std=gnu99: the standard the daemon is written to should be a choice, not
# whatever the first compiler happened to allow.
add_compile_definitions(_POSIX_C_SOURCE=200809L _DEFAULT_SOURCE)

add_compile_options(
  -Wall -Wextra -Werror
  -Wno-unused-parameter
  -fno-common
  # DETERMINISM, not pedantry. GNU C defaults to -ffp-contract=fast, which
  # lets the compiler fuse `a*b + c` into an FMA with a single rounding. The
  # portable detector is a transcription of a numpy oracle that rounds twice,
  # so a contracted multiply-add is a different detector — and it would
  # differ between x86-64 and the board's ARM VFP, which is exactly the
  # divergence D8 exists to measure rather than to acquire by accident.
  -ffp-contract=off
)

# nanopb: pinned 0.4.9, vendored under third_party/. Its bounds come from
# proto/skyweave.options via the checked-in generated sources, so the C
# daemon and the Python host allocate from ONE declaration.
set(NANOPB_DIR ${CMAKE_CURRENT_SOURCE_DIR}/third_party/nanopb)

add_library(skyweave_wire STATIC
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_common.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_wire.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_obsfixture.c
  ${CMAKE_CURRENT_SOURCE_DIR}/proto/skyweave.pb.c
  ${NANOPB_DIR}/pb_common.c
  ${NANOPB_DIR}/pb_encode.c
  ${NANOPB_DIR}/pb_decode.c
)
target_include_directories(skyweave_wire PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/include
  ${CMAKE_CURRENT_SOURCE_DIR}/proto
  ${NANOPB_DIR}
)

set(DAEMON_SOURCES
  ${CMAKE_CURRENT_SOURCE_DIR}/src/main.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_config.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_detect_soft.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_detect_ive.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_pipeline.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_inject.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_net.c
  ${CMAKE_CURRENT_SOURCE_DIR}/src/sw_capture.c
)

add_executable(skyweave-edge ${DAEMON_SOURCES})
target_link_libraries(skyweave-edge PRIVATE skyweave_wire m)

add_executable(sw-fixture-tool ${CMAKE_CURRENT_SOURCE_DIR}/tools/sw_fixture_tool.c)
target_link_libraries(sw-fixture-tool PRIVATE skyweave_wire)

# ---------------------------------------------------------------------------
# The board build. SKYWEAVE_SDK points at a Luckfox SDK checkout; without it
# the RKMPI/IVE/RGA paths are compiled as REFUSALS, never as fallbacks, so a
# host binary can never quietly stand in for a board one.
# ---------------------------------------------------------------------------
if(SKYWEAVE_SDK)
  set(IVE_DIR ${SKYWEAVE_SDK}/media/ive/ive)
  set(RGA_DIR
      ${SKYWEAVE_SDK}/media/rga/release_rga_rv1106_arm-rockchip830-linux-uclibcgnueabihf)
  set(ROCKIT_DIR ${SKYWEAVE_SDK}/media/rockit/rockit)

  foreach(required ${IVE_DIR}/include ${RGA_DIR}/include/rga ${ROCKIT_DIR}/mpi/sdk/include)
    if(NOT EXISTS ${required})
      message(FATAL_ERROR
        "SKYWEAVE_SDK is set but ${required} is missing. Refusing to build a "
        "half-board daemon: a missing header would silently disable a hardware "
        "path.")
    endif()
  endforeach()

  target_compile_definitions(skyweave-edge PRIVATE SKYWEAVE_HAVE_RKMPI=1)
  # No RPATH. CMake would otherwise bake the BUILD CONTAINER's SDK paths
  # (/opt/luckfox-pico/...) into the binary, and those do not exist on the
  # node — the daemon would fail to start on the board while linking
  # perfectly in the container, which is the worst place to find out.
  set_target_properties(skyweave-edge PROPERTIES SKIP_BUILD_RPATH TRUE
                                                 BUILD_WITH_INSTALL_RPATH TRUE
                                                 INSTALL_RPATH "")
  target_include_directories(skyweave-edge PRIVATE
    ${IVE_DIR}/include
    ${RGA_DIR}/include/rga
    ${ROCKIT_DIR}/mpi/sdk/include
  )
  target_link_directories(skyweave-edge PRIVATE
    ${IVE_DIR}/lib
    ${RGA_DIR}/lib
    ${ROCKIT_DIR}/lib/lib32
  )
  target_link_libraries(skyweave-edge PRIVATE rve ivs rga rockit pthread dl rt m)
  # librockit itself needs libdrm and librockchip_mpp, which live on the
  # BOARD's rootfs and are not in the SDK's sparse checkout. Allowing
  # undefined symbols in a SHARED LIBRARY lets the dynamic linker resolve
  # them on the device, which is where they exist. It does not relax linking
  # of this daemon's own symbols: an undefined symbol in our objects is still
  # a link error, which is the check that matters.
  target_link_options(skyweave-edge PRIVATE -Wl,--allow-shlib-undefined)
  message(STATUS "board build: RKMPI/IVE/RGA enabled from ${SKYWEAVE_SDK}")
else()
  message(STATUS "host build: RKMPI/IVE/RGA paths compile as refusals "
                 "(-DSKYWEAVE_SDK=<path> for the board build)")
endif()

install(TARGETS skyweave-edge sw-fixture-tool RUNTIME DESTINATION bin)
