#!/usr/bin/env bash
set -euo pipefail

data_root="${XDG_DATA_HOME:-${HOME}/.local/share}"
config_root="${XDG_CONFIG_HOME:-${HOME}/.config}"
app_dir="$data_root/tapo-cloud-viewer"
desktop_file="$data_root/applications/io.github.filirnd.topo.desktop"
watch_desktop_file="$data_root/applications/io.github.filirnd.topo.watch.desktop"
autostart_file="$config_root/autostart/io.github.filirnd.topo.watch.desktop"
legacy_desktop_file="$data_root/applications/it.topo.TapoCloudView.desktop"
legacy_desktop_file_v1="$data_root/applications/tapo-cloud-viewer.desktop"
icon_file="$data_root/icons/hicolor/scalable/apps/topo-tapo-cloud-view.png"

if [ -e "$app_dir" ]; then
  rm -rf -- "$app_dir"
fi
if [ -e "$desktop_file" ]; then
  rm -f -- "$desktop_file"
fi
if [ -e "$watch_desktop_file" ]; then
  rm -f -- "$watch_desktop_file"
fi
if [ -e "$autostart_file" ]; then
  rm -f -- "$autostart_file"
fi
if [ -e "$legacy_desktop_file" ]; then
  rm -f -- "$legacy_desktop_file"
fi
if [ -e "$legacy_desktop_file_v1" ]; then
  rm -f -- "$legacy_desktop_file_v1"
fi
if [ -e "$icon_file" ]; then
  rm -f -- "$icon_file"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "${data_root}/applications" >/dev/null 2>&1 || true
fi

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user disable --now topo-watch.service >/dev/null 2>&1 || true
  rm -f -- "$config_root/systemd/user/topo-watch.service"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
fi

printf 'Topo - desktop app for Tapo camera uninstalled.\n'
