#!/bin/bash
# Build "Sponge Screener.app" in ~/Applications and put it in the Dock.
#
# The app asks launchd to run the screener server, restarts it when the code
# changed since it started, and opens Chrome on it. macOS stops any process an
# app leaves behind when the app exits, so the server has to be a launchd
# service rather than a child of the app. Run this script again after moving
# the project folder; doing so stops a running server.
set -euo pipefail

PROJECT="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$HOME/Applications"
APP="$APP_DIR/Sponge Screener.app"
LABEL="edu.uvi.vicar.sponge-screener.server"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PORT="$(python3 -c 'import sys; sys.path.insert(0, sys.argv[1]); from screener import config; print(config.PORT)' "$PROJECT")"

# The service starts only when the app asks and stays stopped once stopped.
# Removing the old one first makes launchd read the new definition.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
mkdir -p "$(dirname "$AGENT_PLIST")"
python3 - "$AGENT_PLIST" "$LABEL" "$PROJECT" <<'PY'
import plistlib
import sys

path, label, project = sys.argv[1:]
log = f"{project}/data/screener.log"
service = {
    "Label": label,
    "ProgramArguments": ["/usr/bin/env", "python3", f"{project}/screener.py", "--no-browser"],
    "WorkingDirectory": project,
    "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"},
    "StandardOutPath": log,
    "StandardErrorPath": log,
    "ProcessType": "Interactive",
}
with open(path, "wb") as handle:
    plistlib.dump(service, handle)
PY

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>Sponge Screener</string>
  <key>CFBundleDisplayName</key><string>Sponge Screener</string>
  <key>CFBundleIdentifier</key><string>edu.uvi.vicar.sponge-screener</string>
  <key>CFBundleVersion</key><string>1.1.0</string>
  <key>CFBundleShortVersionString</key><string>1.1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict>
</plist>
PLIST

# The launcher is written with placeholders, then filled in, so its own
# variables need no escaping.
cat > "$APP/Contents/MacOS/launch" <<'LAUNCH'
#!/bin/bash
# Start Sponge Screener and open it in Chrome. A server that started before
# the code last changed is restarted, so the app always runs the newest code.
PROJECT=__PROJECT__
PORT="__PORT__"
LABEL="__LABEL__"
DOMAIN="gui/$(id -u)"
SERVICE="$DOMAIN/$LABEL"
AGENT_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
URL="http://127.0.0.1:$PORT/"
LOG="$PROJECT/data/screener.log"
START_WAIT_TENTHS=200
STOP_WAIT_TENTHS=50

# True when the screener answers its health check.
healthy() { curl -s -m 2 "$URL/api/health" | grep -q '"ok": true'; }

# The process listening on the screener port, or nothing.
server_pid() { lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1; }

# Newest modification time, in epoch seconds, of the code and config the server loads at start.
code_mtime() {
  find "$PROJECT/screener.py" "$PROJECT/screener" "$PROJECT/config" -type f \( -name '*.py' -o -name '*.csv' \) \
    -exec stat -f %m {} + 2>/dev/null | sort -n | tail -1
}

# Start time of a process, in epoch seconds.
started_at() { date -j -f "%a %b %d %T %Y" "$(ps -o lstart= -p "$1" | sed 's/  */ /g')" +%s 2>/dev/null; }

mkdir -p "$PROJECT/data"
pid="$(server_pid)"
if [ -n "$pid" ] && healthy; then
  started="$(started_at "$pid")"
  if [ -n "$started" ] && [ "$(code_mtime)" -gt "$started" ]; then
    kill "$pid"
    for _ in $(seq 1 "$STOP_WAIT_TENTHS"); do [ -z "$(server_pid)" ] && break; sleep 0.1; done
  fi
fi
if ! healthy; then
  launchctl print "$SERVICE" > /dev/null 2>&1 || launchctl bootstrap "$DOMAIN" "$AGENT_PLIST" >> "$LOG" 2>&1
  launchctl kickstart "$SERVICE" >> "$LOG" 2>&1
  for _ in $(seq 1 "$START_WAIT_TENTHS"); do healthy && break; sleep 0.1; done
  if ! healthy; then
    osascript -e "display alert \"Sponge Screener did not start\" message \"The reason is at the end of $LOG.\""
    exit 1
  fi
fi
open -a "Google Chrome" "$URL" 2>/dev/null || open "$URL"
LAUNCH
python3 - "$APP/Contents/MacOS/launch" "$PROJECT" "$PORT" "$LABEL" <<'PY'
import shlex
import sys

path, project, port, label = sys.argv[1:]
with open(path) as handle:
    text = handle.read()
text = text.replace("__PROJECT__", shlex.quote(project)).replace("__PORT__", port).replace("__LABEL__", label)
with open(path, "w") as handle:
    handle.write(text)
PY
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

# Add to the Dock once, then restart the Dock so it shows. The Dock stores the
# path URL-encoded (Sponge%20Screener.app) next to the bundle identifier, so the
# check matches either form.
if ! defaults read com.apple.dock persistent-apps 2>/dev/null \
    | grep -q -e "edu.uvi.vicar.sponge-screener" -e "Sponge Screener.app" -e "Sponge%20Screener.app"; then
  defaults write com.apple.dock persistent-apps -array-add "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>$APP</string><key>_CFURLStringType</key><integer>0</integer></dict></dict></dict>"
  killall Dock
fi

echo "Installed $APP and added it to the Dock."
