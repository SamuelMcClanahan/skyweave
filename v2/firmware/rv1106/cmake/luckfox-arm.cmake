# CMake toolchain file for the Luckfox RV1106 SDK's cross compiler.
#
# The SDK ships an x86_64-hosted gcc 8.3.0 targeting
# arm-rockchip830-linux-uclibcgnueabihf. The container in ../docker/ puts it
# on PATH at a pinned SDK revision; SKYWEAVE_TOOLCHAIN_ROOT overrides that
# for a local SDK checkout.
#
# The -mcpu line is the RV1106's actual core, not a generic ARMv7: a Cortex-A7
# with NEON and a VFPv4 FPU. It matters here beyond speed — the portable
# detector's float arithmetic must round the way the host's does, which is
# why -ffp-contract=off is set in the top-level CMakeLists and why nothing
# below may add -ffast-math.

set(CMAKE_SYSTEM_NAME Linux)
set(CMAKE_SYSTEM_PROCESSOR arm)

if(NOT DEFINED SKYWEAVE_TOOLCHAIN_PREFIX)
  set(SKYWEAVE_TOOLCHAIN_PREFIX arm-rockchip830-linux-uclibcgnueabihf)
endif()

if(DEFINED SKYWEAVE_TOOLCHAIN_ROOT)
  set(_sw_bin "${SKYWEAVE_TOOLCHAIN_ROOT}/bin/")
else()
  set(_sw_bin "")  # rely on PATH (the container does this)
endif()

set(CMAKE_C_COMPILER   "${_sw_bin}${SKYWEAVE_TOOLCHAIN_PREFIX}-gcc")
set(CMAKE_CXX_COMPILER "${_sw_bin}${SKYWEAVE_TOOLCHAIN_PREFIX}-g++")
set(CMAKE_AR           "${_sw_bin}${SKYWEAVE_TOOLCHAIN_PREFIX}-ar")
set(CMAKE_STRIP        "${_sw_bin}${SKYWEAVE_TOOLCHAIN_PREFIX}-strip")

set(SKYWEAVE_ARCH_FLAGS "-mcpu=cortex-a7 -mfpu=neon-vfpv4 -mfloat-abi=hard")
set(CMAKE_C_FLAGS_INIT "${SKYWEAVE_ARCH_FLAGS}")
set(CMAKE_CXX_FLAGS_INIT "${SKYWEAVE_ARCH_FLAGS}")

# Look for headers and libraries in the SDK sysroot, and for programs on the
# host. Without this, find_library() would happily hand the cross build an
# x86-64 .so from the build container.
set(CMAKE_FIND_ROOT_PATH_MODE_PROGRAM NEVER)
set(CMAKE_FIND_ROOT_PATH_MODE_LIBRARY ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_INCLUDE ONLY)
set(CMAKE_FIND_ROOT_PATH_MODE_PACKAGE ONLY)
