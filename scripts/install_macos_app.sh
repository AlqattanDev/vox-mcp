#!/bin/zsh
# Build Vox.app, install it to ~/Applications (clean — never nested), and
# restart the runtime so the new menu bar + Python code go live.
#
# `cp -R src dst` copies INTO dst when dst already exists, nesting the app and
# leaving launchd running the stale binary. This script removes the old app
# first so that can't happen.
set -euo pipefail

ROOT="${0:A:h:h}"
APP="$HOME/Applications/Vox.app"

BUILT="$(zsh "$ROOT/scripts/build_macos_app.sh" | tail -1)"

rm -rf "$APP"
mkdir -p "$HOME/Applications"
cp -R "$BUILT" "$APP"

# Guard against a nested install ever slipping through again.
if [[ -e "$APP/Vox.app" ]]; then
  echo "error: nested app at $APP/Vox.app — install is corrupt" >&2
  exit 1
fi

launchctl kickstart -k "gui/$(id -u)/com.vox.runtime"
echo "installed and restarted: $APP"
