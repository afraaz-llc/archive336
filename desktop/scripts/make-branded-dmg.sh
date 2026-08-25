#!/usr/bin/env bash
#
# Build a branded ARCHIVE336 installer DMG:
#   - a distinct VOLUME icon (the download-arrow "installer" mark) so the
#     mounted disk on the desktop doesn't look like a clone of the app
#   - a "drag me -> Applications" window background
#   - an "Install ARCHIVE336" volume name
#
# Tauri's own dmg target reuses the APP icon for the volume (why the stock
# installer looked like a twin of the app), so we drive Tauri's vendored
# bundle_dmg.sh directly for full control (--volicon / --background / layout).
#
# Prereq: a release .app must already exist (npm run tauri build).
# Run from the desktop/ dir:  ./scripts/make-branded-dmg.sh
set -euo pipefail
cd "$(dirname "$0")/.."            # -> desktop/
ROOT="$PWD"
ASSETS="$ROOT/installer-assets"
BUNDLE="$ROOT/src-tauri/target/release/bundle"
APP="$BUNDLE/macos/ARCHIVE336.app"
BUNDLE_DMG="$BUNDLE/dmg/bundle_dmg.sh"
OUT="$BUNDLE/dmg/ARCHIVE336-Installer.dmg"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

[ -d "$APP" ] || { echo "!! no app at $APP - run 'npm run tauri build' first"; exit 1; }
[ -f "$BUNDLE_DMG" ] || { echo "!! bundle_dmg.sh missing - run a normal tauri build once to vendor it"; exit 1; }

echo "==> volume icon (.icns)"
ICONSET="$WORK/i.iconset"; mkdir -p "$ICONSET"
qlmanage -t -s 1024 -o "$WORK" "$ASSETS/installer-icon.svg" >/dev/null 2>&1
# qlmanage flattens transparency onto white, so the SVG's rounded corners come
# out opaque white. Re-apply a rounded-rect alpha mask (rx=40 on the 200 viewBox
# = 20% radius) so the corners are actually transparent on the desktop.
M="$WORK/icon-master.png"
python3 - "$WORK/installer-icon.svg.png" "$M" <<'PY'
import sys
from PIL import Image, ImageDraw
src, dst = sys.argv[1], sys.argv[2]
S = 1024
im = Image.open(src).convert("RGBA").resize((S, S), Image.LANCZOS)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(40 / 200 * S), fill=255)
im.putalpha(mask)
im.save(dst)
PY
for s in 16 32 128 256 512; do
  sips -z "$s" "$s" "$M" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z "$((s*2))" "$((s*2))" "$M" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$WORK/installer.icns"

echo "==> window background (600x420)"
qlmanage -t -s 600 -o "$WORK" "$ASSETS/dmg-background.svg" >/dev/null 2>&1
# qlmanage letterboxes the 600x420 art into a 600x600 square; crop the black
# padding back off so the content lands where the icons are placed.
sips -c 420 600 "$WORK/dmg-background.svg.png" --out "$WORK/bg.png" >/dev/null

echo "==> stage app"
STAGE="$WORK/stage"; mkdir -p "$STAGE"
cp -R "$APP" "$STAGE/"

echo "==> build DMG"
rm -f "$OUT"
"$BUNDLE_DMG" \
  --volname "Install ARCHIVE336" \
  --volicon "$WORK/installer.icns" \
  --background "$WORK/bg.png" \
  --window-size 600 420 \
  --icon-size 120 \
  --text-size 13 \
  --icon "ARCHIVE336.app" 150 205 \
  --app-drop-link 450 205 \
  "$OUT" "$STAGE"

echo "==> done: $OUT"
