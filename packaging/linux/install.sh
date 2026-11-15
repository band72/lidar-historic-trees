#!/usr/bin/env bash
# LiDAR Tree Inventory - Linux installer.
#
#   ./install.sh            install for the current user (no root needed)
#   sudo ./install.sh --system   install for all users into /opt
#   ./install.sh --prefix DIR    install into DIR
#   ./install.sh --no-desktop    skip the desktop icon
#
# Installs the program, a `fip` command on PATH, a menu entry, a desktop icon and an uninstaller.
set -euo pipefail

APP_ID="lidar-tree-inventory"
APP_NAME="LiDAR Tree Inventory"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/$APP_ID"

MODE="user"
PREFIX=""
DESKTOP_ICON=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --system) MODE="system"; shift ;;
    --prefix) PREFIX="${2:?--prefix needs a directory}"; shift 2 ;;
    --no-desktop) DESKTOP_ICON=0; shift ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

die() { echo "error: $*" >&2; exit 1; }

[[ -x "$SRC/fip" ]] || die "program files not found next to install.sh ($SRC/fip). Extract the whole archive first."
[[ "$(uname -s)" == "Linux" ]] || die "this installer is for Linux"
[[ "$(uname -m)" == "x86_64" ]] || die "this build is for x86_64; got $(uname -m)"

# Open3D (CPU build) needs glibc >= 2.35 (Ubuntu 22.04, Debian 12, Fedora 36 or newer)
GLIBC="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')"
if [[ -n "$GLIBC" ]] && [[ "$(printf '%s\n2.35\n' "$GLIBC" | sort -V | head -1)" != "2.35" ]]; then
  die "glibc $GLIBC is too old; need 2.35+ (Ubuntu 22.04 / Debian 12 / Fedora 36 or newer)"
fi

if [[ "$MODE" == "system" ]]; then
  [[ $EUID -eq 0 ]] || die "--system needs root: sudo ./install.sh --system"
  PREFIX="${PREFIX:-/opt/$APP_ID}"
  BIN_DIR="/usr/local/bin"
  DESKTOP_DIR="/usr/share/applications"
  ICON_DIR="/usr/share/icons/hicolor/256x256/apps"
else
  PREFIX="${PREFIX:-${XDG_DATA_HOME:-$HOME/.local/share}/$APP_ID}"
  BIN_DIR="$HOME/.local/bin"
  DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"
fi

echo "Installing $APP_NAME"
echo "  program : $PREFIX"
echo "  command : $BIN_DIR/fip"

if [[ -e "$PREFIX" ]]; then
  [[ -f "$PREFIX/.fip-install" ]] || die "$PREFIX exists and was not created by this installer; choose another --prefix"
  echo "  (replacing the existing installation)"
  rm -rf "$PREFIX"
fi
mkdir -p "$PREFIX" "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR"
cp -a "$SRC/." "$PREFIX/"
cp "$HERE/VERSION" "$PREFIX/VERSION" 2>/dev/null || true
echo "$MODE" > "$PREFIX/.fip-install"

ln -sf "$PREFIX/fip" "$BIN_DIR/fip"
cp "$HERE/icon.png" "$ICON_DIR/$APP_ID.png"

cat > "$DESKTOP_DIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Find trees in LiDAR point clouds and measure DBH with confidence intervals
Exec="$PREFIX/fip"
Icon=$APP_ID
Terminal=true
Categories=Science;Education;Geography;
Keywords=lidar;forest;tree;dbh;point cloud;
EOF

# desktop icon (the invoking user's desktop, also for sudo --system installs)
DESKTOP_FILE=""
if [[ "$DESKTOP_ICON" == 1 ]]; then
  REAL_USER="${SUDO_USER:-$USER}"
  REAL_HOME="$(getent passwd "$REAL_USER" | cut -d: -f6)"
  as_user() { if [[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]]; then sudo -u "$REAL_USER" "$@"; else "$@"; fi; }
  DESK="$(as_user xdg-user-dir DESKTOP 2>/dev/null || true)"
  [[ -n "$DESK" && "$DESK" != "$REAL_HOME" ]] || DESK="$REAL_HOME/Desktop"
  if [[ -d "$DESK" ]]; then
    DESKTOP_FILE="$DESK/$APP_ID.desktop"
    cp "$DESKTOP_DIR/$APP_ID.desktop" "$DESKTOP_FILE"
    sed -i "s|^Icon=.*|Icon=$ICON_DIR/$APP_ID.png|" "$DESKTOP_FILE"
    chmod +x "$DESKTOP_FILE"
    [[ $EUID -eq 0 && -n "${SUDO_USER:-}" ]] && chown "$REAL_USER": "$DESKTOP_FILE"
    # GNOME only launches desktop files marked as trusted
    if command -v gio >/dev/null; then
      as_user gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
    fi
    echo "  icon    : $DESKTOP_FILE"
  fi
fi

cat > "$PREFIX/uninstall.sh" <<EOF
#!/usr/bin/env bash
# Removes $APP_NAME installed by install.sh ($MODE install).
set -euo pipefail
rm -f "$BIN_DIR/fip" "$DESKTOP_DIR/$APP_ID.desktop" "$ICON_DIR/$APP_ID.png" ${DESKTOP_FILE:+"$DESKTOP_FILE"}
rm -rf "$PREFIX"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
echo "$APP_NAME removed. Logs and outputs you created were not touched."
EOF
chmod +x "$PREFIX/uninstall.sh"

command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true

echo
echo "Checking the installation (fip self-test --skip-app)..."
if "$PREFIX/fip" self-test --skip-app >/dev/null 2>&1; then
  echo "  OK"
else
  echo "  warning: self-test failed; run '$PREFIX/fip self-test' to see why" >&2
fi

echo
echo "Done."
echo "  Start the app : '$APP_NAME' in your applications menu, or run: fip"
echo "  Command line  : fip --help"
echo "  Uninstall     : $PREFIX/uninstall.sh"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  note: $BIN_DIR is not on your PATH; add it, e.g.  echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc" ;;
esac
