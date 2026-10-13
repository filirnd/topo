#!/usr/bin/env bash
set -euo pipefail

data_root="${XDG_DATA_HOME:-${HOME}/.local/share}"
app_dir="$data_root/tapo-cloud-viewer"
desktop_file="$data_root/applications/it.topo.TapoCloudView.desktop"
legacy_desktop_file="$data_root/applications/tapo-cloud-viewer.desktop"
icon_file="$data_root/icons/hicolor/scalable/apps/topo-tapo-cloud-view.png"

if [ -e "$app_dir" ]; then
  rm -rf -- "$app_dir"
fi
if [ -e "$desktop_file" ]; then
  rm -f -- "$desktop_file"
fi
if [ -e "$legacy_desktop_file" ]; then
  rm -f -- "$legacy_desktop_file"
fi
if [ -e "$icon_file" ]; then
  rm -f -- "$icon_file"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${data_root}/applications" >/dev/null 2>&1 || true
fi

printf 'Topo - desktop app for Tapo camera uninstalled.\n'
