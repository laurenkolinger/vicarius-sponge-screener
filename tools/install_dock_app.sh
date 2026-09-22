#!/bin/bash
# Build "Sponge Screener.app" in ~/Applications and put it in the Dock.
#
# The app starts the screener server if it is not already running and opens
# Chrome on it. Run this script again after moving the project folder.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/Sponge Screener.app"
PORT="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from screener import config; print(config.PORT)' "$PROJECT")"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Sponge Screener</string>
  <key>CFBundleDisplayName</key><string>Sponge Screener</string>
  <key>CFBundleIdentifier</key><string>edu.uvi.vicar.sponge-screener</string>
  <key>CFBundleVersion</key><string>1.0.0</string>
  <key>CFBundleShortVersionString</key><string>1.0.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict>
</plist>
PLIST

cat > "$APP/Contents/MacOS/launch" <<LAUNCH
#!/bin/bash
# Start Sponge Screener, or bring it up in Chrome when it is already running.
export PATH="/opt/homebrew/bin:/usr/local/bin:\$PATH"
PROJECT="$PROJECT"
URL="http://127.0.0.1:$PORT/"
if curl -s -m 2 "\$URL/api/health" | grep -q '"ok": true'; then
  open -a "Google Chrome" "\$URL" 2>/dev/null || open "\$URL"
  exit 0
fi
mkdir -p "\$PROJECT/data"
cd "\$PROJECT"
nohup python3 "\$PROJECT/screener.py" >> "\$PROJECT/data/screener.log" 2>&1 &
LAUNCH
chmod +x "$APP/Contents/MacOS/launch"

# Icon: one PNG drawn in Python, resized into the sizes an .icns needs.
ICONSET="$(mktemp -d)/icon.iconset"
mkdir -p "$ICONSET"
python3 "$PROJECT/tools/make_icon.py" "$ICONSET/icon_512x512@2x.png" 1024
for pair in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" "64 icon_32x32@2x" \
            "128 icon_128x128" "256 icon_128x128@2x" "256 icon_256x256" "512 icon_256x256@2x" "512 icon_512x512"; do
  set -- $pair
  sips -z "$1" "$1" "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/$2.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/icon.icns"
rm -rf "$(dirname "$ICONSET")"
touch "$APP"

# Add to the Dock once, then restart the Dock so it shows.
if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "Sponge Screener.app"; then
  defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>$APP</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>"
  killall Dock
fi

echo "Installed $APP and added it to the Dock."
