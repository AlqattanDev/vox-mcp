#!/bin/sh
# UserPromptSubmit hook: if Vox is listening when a prompt is submitted, hand the
# typed text to that listen instead of letting it time out.
#
# A running listen blocks the host's turn, so a message typed while the mic is
# open would otherwise sit queued until the capture ended on its own — the user
# waiting out a microphone they had already decided not to use. This ends the
# listen at once and the typed text becomes the turn.
#
# A no-op unless the microphone is actually open, so the ordinary case costs one
# loopback health check. Never fails the prompt: every path exits 0, because a
# voice convenience must not be able to block Ali from talking to the agent.

set -u

HEALTH_URL="http://127.0.0.1:8766/health"
CONTROL_URL="http://127.0.0.1:8766/control"
TOKEN_FILE="${VOX_HOME:-$HOME/.vox}/control.token"
LOG="${VOX_HOME:-$HOME/.vox}/state/deliver_text_hook.log"

payload=$(cat)

note() {
    printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" >>"$LOG" 2>/dev/null || true
}

# The mic has to be open right now. Without this every prompt would try to
# deliver text into a listen that does not exist.
health=$(curl -s --max-time 1 "$HEALTH_URL" 2>/dev/null) || exit 0
case "$health" in
    *'"microphone_open":true'*) ;;
    *) exit 0 ;;
esac

[ -r "$TOKEN_FILE" ] || { note "mic open but no readable control token"; exit 0; }
token=$(cat "$TOKEN_FILE")

prompt=$(printf '%s' "$payload" | /usr/bin/python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("prompt", "") or "", end="")
except Exception:
    pass
' 2>/dev/null)

[ -n "$prompt" ] || exit 0

body=$(/usr/bin/python3 -c '
import json, sys
print(json.dumps({"action": "deliver_text", "text": sys.argv[1]}))
' "$prompt" 2>/dev/null) || exit 0

result=$(curl -s --max-time 2 -X POST "$CONTROL_URL" \
    -H "Authorization: Bearer $token" \
    -H "Content-Type: application/json" \
    -d "$body" 2>/dev/null)

note "mic was open; delivered ${#prompt} chars; runtime said: ${result:-<no response>}"
exit 0
