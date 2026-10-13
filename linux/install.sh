#!/usr/bin/env bash
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
data_root="${XDG_DATA_HOME:-${HOME}/.local/share}"
app_dir="$data_root/tapo-cloud-viewer"
applications_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
icon_dir="$data_root/icons/hicolor/scalable/apps"
desktop_file="$applications_dir/it.topo.TapoCloudView.desktop"
legacy_desktop_file="$applications_dir/tapo-cloud-viewer.desktop"

die() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

runtime_ok() {
  command -v python3 >/dev/null 2>&1 || return 1
  command -v ffmpeg >/dev/null 2>&1 || return 1
  python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2
PY
}

detect_package_manager() {
  local distro_id="" distro_like=""
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    distro_id="${ID:-}"
    distro_like="${ID_LIKE:-}"
  fi

  if [[ "$distro_id" == "ubuntu" || "$distro_id" == "debian" || "$distro_like" == *debian* ]]; then
    printf 'apt\n'
  elif [[ "$distro_id" == "fedora" || "$distro_id" == "rhel" || "$distro_id" == "centos" || "$distro_id" == "rocky" || "$distro_id" == "almalinux" || "$distro_like" == *fedora* || "$distro_like" == *rhel* ]]; then
    printf 'dnf\n'
  else
    printf 'unknown\n'
  fi
}

install_dependencies() {
  local package_manager="$1"
  local -a privilege=()
  if (( EUID != 0 )); then
    command -v sudo >/dev/null 2>&1 || die 'sudo is not available; install dependencies as root.'
    privilege=(sudo)
  fi
  case "$package_manager" in
    apt)
      printf '%s\n' 'Required dependencies: python3, python3-gi, GTK 3, WebKitGTK 4.1, FFmpeg.'
      "${privilege[@]}" apt-get update
      "${privilege[@]}" apt-get install -y \
        python3 python3-gi gir1.2-gtk-3.0 gir1.2-webkit2-4.1 ffmpeg
      ;;
    dnf)
      printf '%s\n' 'Required dependencies: python3, python3-gobject, GTK 3, WebKitGTK 4.1, FFmpeg.'
      "${privilege[@]}" dnf install -y \
        python3 python3-gobject gtk3 webkit2gtk4.1 ffmpeg
      ;;
    *)
      die 'distribution not recognized. Install Python 3, GTK 3, WebKitGTK 4.1, and FFmpeg manually, then run this script again.'
      ;;
  esac
}

if ! runtime_ok; then
  package_manager="$(detect_package_manager)"
  printf '%s\n' 'One or more required dependencies are missing.'
  case "$package_manager" in
    apt)
      printf '%s\n' 'Detected system: Ubuntu/Debian.'
      ;;
    dnf)
      printf '%s\n' 'Detected system: Fedora/RHEL.'
      ;;
    *)
      die 'distribution not recognized. Install Python 3, GTK 3, WebKitGTK 4.1, and FFmpeg manually, then run linux/install.sh again.'
      ;;
  esac
  read -r -p 'Install dependencies automatically? [y/N] ' answer
  case "$answer" in
    y|Y|yes|YES)
      install_dependencies "$package_manager"
      ;;
    *)
      die 'installation cancelled. Install the required dependencies and run linux/install.sh again.'
      ;;
  esac
  runtime_ok || die 'dependencies are still incomplete after installation.'
fi

mkdir -p "$app_dir/cloud_web" "$applications_dir" "$icon_dir"
rm -f -- "$app_dir/tapo-cloud-viewer.svg" "$icon_dir/topo-tapo-cloud-view.svg"
rm -f -- "$legacy_desktop_file"
cp "$source_dir/tapo_cloud_app.py" "$app_dir/"
cp "$source_dir/tapo_cloud_probe.py" "$app_dir/"
cp "$source_dir/tapo_desktop.py" "$app_dir/"
cp -a "$source_dir/cloud_web/." "$app_dir/cloud_web/"
cp "$source_dir/linux/run-tapo-cloud-viewer" "$app_dir/"
cp "$source_dir/linux/tapo-cloud-viewer.png" "$app_dir/"
cp "$source_dir/linux/tapo-cloud-viewer.png" "$icon_dir/topo-tapo-cloud-view.png"
chmod 755 "$app_dir/run-tapo-cloud-viewer"

escaped_app_dir=$(printf '%s' "$app_dir" | sed 's/[&|]/\\&/g')
sed "s|@APP_DIR@|$escaped_app_dir|g" \
  "$source_dir/linux/tapo-cloud-viewer.desktop.in" \
  > "$desktop_file"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi

printf 'Topo - desktop app for Tapo camera installed. Launch it from the applications menu.\n'
printf 'Installed files: %s\n' "$app_dir"
