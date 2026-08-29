#!/bin/bash
# Builds "GPU Monitor.app" into ~/Applications. Re-run after editing GPUMonitor.swift.
set -euo pipefail
cd "$(dirname "$0")"

APP="$HOME/Applications/GPU Monitor.app"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

echo "→ 아이콘 렌더"
"$CHROME" --headless --disable-gpu --screenshot="$BUILD/icon.png" \
  --window-size=1024,1024 --default-background-color=00000000 --hide-scrollbars \
  "file://$PWD/icon.html" 2>/dev/null

ICONSET="$BUILD/AppIcon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 64 128 256 512; do
  sips -z $s $s "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$BUILD/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$BUILD/AppIcon.icns"

echo "→ 컴파일"
clang -fobjc-arc -O2 -Wall -mmacosx-version-min=13.0 \
  -framework Cocoa -framework WebKit \
  -o "$BUILD/GPUMonitor" GPUMonitor.m

echo "→ 번들 조립"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
mv "$BUILD/GPUMonitor" "$APP/Contents/MacOS/GPUMonitor"
mv "$BUILD/AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>GPU Monitor</string>
  <key>CFBundleDisplayName</key><string>GPU Monitor</string>
  <key>CFBundleIdentifier</key><string>com.cmw9903.gpumonitor</string>
  <key>CFBundleExecutable</key><string>GPUMonitor</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <!-- The dashboard is served over plain http on loopback. -->
  <key>NSAppTransportSecurity</key>
  <dict><key>NSAllowsLocalNetworking</key><true/></dict>
</dict>
</plist>
PLIST

# Unsigned bundles get quarantined/refused; an ad-hoc signature is enough locally.
codesign --force --deep --sign - "$APP" 2>/dev/null || true
touch "$APP"

echo "✓ $APP"
