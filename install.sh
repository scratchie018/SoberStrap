#!/usr/bin/env bash
# Install the built binary as `sober-strap` + an app-menu entry.
set -euo pipefail
cd "$(dirname "$0")"

[[ -x dist/soberstrap ]] || ./build.sh
BIN="$HOME/.local/bin/sober-strap"
DATA="$HOME/.local/share/sober-strap"
APPS="$HOME/.local/share/applications"
mkdir -p "$(dirname "$BIN")" "$DATA" "$APPS"

install -m 755 dist/soberstrap "$BIN"
[[ -f "$DATA/logo.png" ]] || cp assets/logo.png "$DATA/logo.png"

cat > "$APPS/sober-strap.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=SoberStrap
Comment=Launch Sober with FastFlags, Fleasion and fixes
Exec=$BIN
Icon=$DATA/logo.png
Categories=Game;
Terminal=false
StartupWMClass=sober-strap
Actions=launch;fleasion;noproxy;

[Desktop Action launch]
Name=Launch Roblox
Exec=$BIN --launch

[Desktop Action fleasion]
Name=Launch with Fleasion
Exec=$BIN --fleasion

[Desktop Action noproxy]
Name=Launch without proxy
Exec=$BIN --no-proxy
DESKTOP
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "installed: $BIN  (find \"SoberStrap\" in your app menu)"
