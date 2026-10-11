#!/bin/zsh
# Build the two local speech services Vox talks to, at the paths the
# installer expects:
#
#   whisper.cpp  -> $ROOT/whisper   (whisper-server + ggml-small.bin, Metal)
#   Kokoro       -> $ROOT/kokoro    (Kokoro-FastAPI + its v1.0 model, MPS)
#
# $ROOT defaults to ~/.voicemode/services, the layout VoiceMode created and the
# installer reads, so a Mac that already has VoiceMode's services is left as
# is. Every step is skipped when its result already exists, so re-running is
# safe. Nothing here starts a service: `vox install --activate` writes the
# launch agents that do.
#
# Versions are pinned. Upgrading either is a deliberate edit to this file.
set -euo pipefail

ROOT="${VOX_SPEECH_ROOT:-$HOME/.voicemode/services}"
WHISPER_TAG="v1.9.3"
WHISPER_MODEL="small"
KOKORO_TAG="v0.2.4"
KOKORO_PYTHON="3.12"

say() { print -P "%B==>%b $*"; }
die() { print -u2 "error: $*"; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required. $2"; }

[[ "$(uname -s)" == Darwin && "$(uname -m)" == arm64 ]] || die "Vox needs an Apple silicon Mac."
xcode-select -p >/dev/null 2>&1 || die "Xcode Command Line Tools are required. Run: xcode-select --install"
need git "Install the Xcode Command Line Tools: xcode-select --install"
need uv "Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
for tool in cmake ffmpeg; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    need brew "Install Homebrew from https://brew.sh, or install $tool yourself."
    say "Installing $tool with Homebrew"
    brew install "$tool"
  fi
done

mkdir -p "$ROOT"

# ── whisper.cpp ─────────────────────────────────────────────────────────────
W="$ROOT/whisper"
if [[ ! -d "$W/.git" ]]; then
  say "Cloning whisper.cpp $WHISPER_TAG"
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$WHISPER_TAG" https://github.com/ggml-org/whisper.cpp.git "$W"
fi
if [[ ! -x "$W/build/bin/whisper-server" ]]; then
  say "Building whisper-server (Metal)"
  cmake -S "$W" -B "$W/build" -DCMAKE_BUILD_TYPE=Release -DGGML_METAL=ON >/dev/null
  cmake --build "$W/build" --config Release --target whisper-server -j "$(sysctl -n hw.ncpu)" >/dev/null
fi
if [[ ! -s "$W/models/ggml-$WHISPER_MODEL.bin" ]]; then
  say "Downloading the Whisper $WHISPER_MODEL model"
  sh "$W/models/download-ggml-model.sh" "$WHISPER_MODEL" "$W/models" >/dev/null
fi

# ── Kokoro-FastAPI ──────────────────────────────────────────────────────────
K="$ROOT/kokoro"
if [[ ! -d "$K/.git" ]]; then
  say "Cloning Kokoro-FastAPI $KOKORO_TAG"
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$KOKORO_TAG" https://github.com/remsky/kokoro-fastapi.git "$K"
fi
if [[ ! -x "$K/.venv/bin/python" ]]; then
  say "Creating Kokoro's Python $KOKORO_PYTHON environment"
  (cd "$K" && uv venv --quiet --python "$KOKORO_PYTHON" .venv)
fi
if ! "$K/.venv/bin/python" -c "import uvicorn, kokoro" >/dev/null 2>&1; then
  say "Installing Kokoro (this downloads PyTorch; a few minutes)"
  (cd "$K" && VIRTUAL_ENV="$K/.venv" uv pip install --quiet -e .)
fi
if [[ ! -s "$K/api/src/models/v1_0/kokoro-v1_0.pth" ]]; then
  say "Downloading the Kokoro v1.0 model"
  (cd "$K" && "$K/.venv/bin/python" docker/scripts/download_model.py --output api/src/models/v1_0)
fi

say "Speech services ready under $ROOT"
print "  whisper-server: $W/build/bin/whisper-server"
print "  whisper model:  $W/models/ggml-$WHISPER_MODEL.bin"
print "  kokoro python:  $K/.venv/bin/python"
