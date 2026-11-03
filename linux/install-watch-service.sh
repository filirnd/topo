#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
autostart_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/autostart"
unit_name="topo-watch.service"

mkdir -p "$unit_dir"
escaped_app_dir=$(printf '%s' "$source_dir" | sed 's/[&|]/\\&/g')
sed "s|@APP_DIR@|$escaped_app_dir|g" \
  "$source_dir/linux/topo-watch.service" > "$unit_dir/$unit_name"

# Prefer the systemd user service over a desktop autostart entry.
rm -f -- "$autostart_dir/io.github.filirnd.topo.watch.desktop"

systemctl --user daemon-reload
systemctl --user enable --now "$unit_name"

printf 'Topo Watcher installed as a user systemd service (headless).\n'
printf 'Commands:\n'
printf '  systemctl --user status %s\n' "$unit_name"
printf '  systemctl --user restart %s\n' "$unit_name"
printf '  journalctl --user -u %s -f\n' "$unit_name"
