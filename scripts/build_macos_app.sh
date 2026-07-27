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
# macOS pins TCC grants to the code signature. Ad-hoc signing mints a new
# cdhash on every build, so an ad-hoc fallback silently revokes Accessibility
# — and with it dictation and read-aloud — on every single deploy, with no
# error anywhere to explain why the feature just stopped. Fail the build
# instead. The previous pattern also only matched "Apple Development:", so a
# Developer ID certificate fell through to ad-hoc without saying so.
IDENTITY="${VOX_CODESIGN_IDENTITY:-}"
if [[ -z "$IDENTITY" ]]; then
  IDENTITY="$(/usr/bin/security find-identity -v -p codesigning 2>/dev/null \
    | /usr/bin/awk -F '"' '/Apple Development:|Developer ID Application:|Apple Distribution:/{print $2; exit}')"
fi
if [[ -n "$IDENTITY" ]]; then
  /usr/bin/codesign --force --deep --timestamp=none --sign "$IDENTITY" "$APP"
elif [[ -n "${VOX_ALLOW_ADHOC_SIGN:-}" ]]; then
  echo "warning: ad-hoc signing by request; Accessibility must be re-granted after every build" >&2
  /usr/bin/codesign --force --deep --sign - "$APP"
else
  echo "error: no persistent codesigning identity found." >&2
  echo "  Accessibility (dictation, read-aloud) is pinned to the signature and would be" >&2
  echo "  revoked on every install. Set VOX_CODESIGN_IDENTITY, or VOX_ALLOW_ADHOC_SIGN=1" >&2
  echo "  to build without those features working across deploys." >&2
  exit 1
fi
/usr/bin/plutil -lint "$CONTENTS/Info.plist"
echo "$APP"
