#!/bin/zsh
# One command from a fresh clone to a running Vox:
#
#   git clone https://github.com/AlqattanDev/vox-mcp.git ~/vox-mcp
#   ~/vox-mcp/install.sh
#
# 1. builds the local speech services (scripts/setup_speech.sh),
# 2. builds and signs Vox.app,
# 3. installs the Python runtime (which bundles that app) from the frozen lock,
# 4. prints the installer's dry-run plan, then activates it: launch agents for
#    the runtime and both speech services, the menu-bar app, and the MCP entry
#    for Claude Code and Codex when they are installed.
#
# The launch agent runs the runtime from this checkout, so keep the clone
# where it is. Re-running is safe; `vox install` keeps a rollback snapshot.
set -euo pipefail

ROOT="${0:A:h}"
cd "$ROOT"

say() { print -P "%B==>%b $*"; }
die() { print -u2 "error: $*"; exit 1; }

zsh scripts/setup_speech.sh

# macOS ties the Accessibility grant (dictation, read-aloud) to the app's
# signature, so the build wants a stable signing identity. A free Apple ID in
# Xcode provides one.
if [[ -z "${VOX_CODESIGN_IDENTITY:-}${VOX_ALLOW_ADHOC_SIGN:-}" ]] &&
  ! /usr/bin/security find-identity -v -p codesigning 2>/dev/null | grep -qE 'Apple Development:|Developer ID Application:|Apple Distribution:'; then
  print -u2 "error: no code-signing identity on this Mac."
  print -u2 "  Free option: open Xcode › Settings › Accounts, add your Apple ID, then"
  print -u2 "  Manage Certificates › + › Apple Development. Re-run ./install.sh."
  print -u2 "  Or run VOX_ALLOW_ADHOC_SIGN=1 ./install.sh — everything works, but"
  print -u2 "  Accessibility has to be granted again after every rebuild."
  exit 1
fi

say "Building Vox.app"
zsh scripts/build_macos_app.sh >/dev/null

# The package bundles dist/Vox.app, so the runtime installs after the build.
say "Installing the Vox runtime"
uv sync --frozen --quiet

say "Installer plan (dry run)"
.venv/bin/vox install

if [[ -z "${VOX_YES:-}" ]]; then
  read -q "?Activate this plan? [y/N] " || { print; die "stopped before activation; nothing was changed"; }
  print
fi
.venv/bin/vox install --activate --yes

say "Checking the installation"
.venv/bin/vox doctor || true

cat <<'EOF'

Vox is installed. Next:
  • Restart Claude Code and Codex so they load the Vox MCP server.
  • Click the Vox item in the menu bar; macOS asks for the microphone on first use.
  • ⌘§ (hold) dictates and ⌘§ (tap) reads the selection; both need Accessibility
    for Vox under System Settings › Privacy & Security › Accessibility.
EOF
