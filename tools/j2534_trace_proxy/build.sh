#!/usr/bin/env bash
# Cross-compile the J2534 logging proxy to a 32-bit Windows DLL with mingw.
#
# A GM factory tool is typically a 32-bit process and loads a 32-bit J2534 DLL,
# so the DEFAULT target here is i686 (32-bit). Build the 64-bit variant too if
# your tool is 64-bit; register whichever matches the tool's bitness.
#
# Needs: i686-w64-mingw32-gcc  (Debian/Ubuntu: apt-get install gcc-mingw-w64-i686)
#        x86_64-w64-mingw32-gcc for the 64-bit build.
# NEXUS (Ubuntu) has apt; paxson has no C toolchain, so build on NEXUS or any box
# with mingw and copy the DLL back.
set -euo pipefail
cd "$(dirname "$0")"

CFLAGS="-O2 -shared -s -Wall -static-libgcc"

echo "== 32-bit =="
i686-w64-mingw32-gcc $CFLAGS -o j2534_proxy_x86.dll j2534_proxy.c j2534_proxy.def
echo "built j2534_proxy_x86.dll"

if command -v x86_64-w64-mingw32-gcc >/dev/null 2>&1; then
  echo "== 64-bit =="
  x86_64-w64-mingw32-gcc $CFLAGS -o j2534_proxy_x64.dll j2534_proxy.c j2534_proxy.def
  echo "built j2534_proxy_x64.dll"
fi
