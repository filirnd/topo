#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
project_root=$(cd -- "$script_dir/.." && pwd)
image_name=${TOPO_WINDOWS_BUILDER_IMAGE:-topo-windows-builder:local}
ffmpeg_url=${TOPO_FFMPEG_URL:-https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip}

if ! command -v docker >/dev/null 2>&1; then
  echo 'Docker is required. Install Docker and retry.' >&2
  exit 1
fi

if ! docker image inspect "$image_name" >/dev/null 2>&1; then
  echo "Building local Windows toolchain image: $image_name"
  docker build \
    --file "$script_dir/Dockerfile.local" \
    --tag "$image_name" \
    "$project_root"
fi

mkdir -p "$script_dir/dist" "$script_dir/out"
if [ ! -s "$script_dir/dist/ffmpeg.exe" ]; then
  temporary_dir=$(mktemp -d)
  trap 'rm -rf "$temporary_dir"' EXIT
  echo 'Downloading Windows FFmpeg runtime...'
  curl --fail --location --retry 3 "$ffmpeg_url" --output "$temporary_dir/ffmpeg.zip"
  unzip -j -o "$temporary_dir/ffmpeg.zip" '*/bin/ffmpeg.exe' -d "$script_dir/dist" >/dev/null
fi

echo 'Building Windows backend and Electron installer locally...'
docker run --rm --init \
  --env WINEDEBUG=-all \
  --volume "$project_root:/project" \
  --workdir /project \
  "$image_name" \
  /project/windows/build-inside-wine.sh

echo
echo "Installer files are in: $script_dir/out"
