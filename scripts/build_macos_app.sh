#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
APP="$ROOT/dist/Vox.app"
CONTENTS="$APP/Contents"
MACOS="$CONTENTS/MacOS"

rm -rf "$APP"
mkdir -p "$MACOS"
/usr/bin/swiftc \
  -parse-as-library \
  -swift-version 5 \
  -target arm64-apple-macosx13.0 \
  -O \
  -framework AppKit \
  -framework AVFoundation \
  -framework Carbon \
  "$ROOT/macos/VoxStatus.swift" \
  -o "$MACOS/VoxStatus"
/bin/cp "$ROOT/macos/Info.plist" "$CONTENTS/Info.plist"
IDENTITY="${VOX_CODESIGN_IDENTITY:-}"
if [[ -z "$IDENTITY" ]]; then
  IDENTITY="$(/usr/bin/security find-identity -v -p codesigning 2>/dev/null | /usr/bin/awk -F '"' '/Apple Development:/{print $2; exit}')"
fi
if [[ -n "$IDENTITY" ]]; then
  /usr/bin/codesign --force --deep --timestamp=none --sign "$IDENTITY" "$APP"
else
  echo "warning: no persistent Apple Development signing identity; using ad-hoc signing" >&2
  /usr/bin/codesign --force --deep --sign - "$APP"
fi
/usr/bin/plutil -lint "$CONTENTS/Info.plist"
echo "$APP"
