#!/usr/bin/env bash
set -euo pipefail

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$script_dir"

if [[ -n "${TOPO_NATIVE_INCLUDE_ROOT:-}" && -n "${TOPO_NATIVE_LIBDIR:-}" ]]; then
  # Useful for an unprivileged local build using development packages
  # extracted from .deb archives.
  cc -std=c11 -O2 -Wall -Wextra -Wpedantic \
    -I"$TOPO_NATIVE_INCLUDE_ROOT/usr/include/x86_64-linux-gnu" \
    -I"$TOPO_NATIVE_INCLUDE_ROOT/usr/include" \
    -I"$TOPO_NATIVE_INCLUDE_ROOT/usr/include/SDL2" \
    native_ffmpeg_player.c \
    -o native-ffmpeg-player \
    -L"$TOPO_NATIVE_LIBDIR" \
    -Wl,-rpath,"$TOPO_NATIVE_LIBDIR" \
    -l:libavformat.so.60 \
    -l:libavcodec.so.60 \
    -l:libavutil.so.58 \
    -l:libswresample.so.4 \
    -l:libswscale.so.7 \
    -l:libSDL2-2.0.so.0 \
    -lm -pthread -ldl
elif pkg-config --exists libavformat libavcodec libavutil libswscale sdl2; then
  cc -std=c11 -O2 -Wall -Wextra -Wpedantic \
    native_ffmpeg_player.c \
    -o native-ffmpeg-player \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswscale sdl2)
else
  echo "Missing development packages: libavformat-dev libavcodec-dev libavutil-dev libswscale-dev libsdl2-dev" >&2
  exit 1
fi

echo "Built $script_dir/native-ffmpeg-player"
