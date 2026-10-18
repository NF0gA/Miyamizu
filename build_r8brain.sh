#!/usr/bin/env bash
# r8brain ラッパー(libr8bshim.so)を事前ビルドする。openSUSE: sudo zypper in gcc-c++
set -e
cd "$(dirname "$0")/miyamizu/native"
g++ -O3 -std=c++17 -shared -fPIC -fvisibility=hidden -Ir8brain-free-src r8b_shim.cpp -o ../libr8bshim.so
echo "built miyamizu/libr8bshim.so"
