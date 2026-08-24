#!/usr/bin/env bash
# Waybar custom module for JARVIS.
# Add the JSON in ~/.jarvis/waybar-module.jsonc to your Waybar config.
set -euo pipefail

PORT="${JARVIS_PORT:-8787}"
URL="http://127.0.0.1:${PORT}/api/health"

if ! out="$(curl -sf --max-time 0.4 "$URL" 2>/dev/null)"; then
  printf '{"text":"J","alt":"offline","class":"offline","tooltip":"JARVIS offline"}\n'
  exit 0
fi

busy="$(printf '%s' "$out" | grep -o '"busy":[^,]*' | head -n1 | grep -c true || true)"
if [[ "$busy" -gt 0 ]]; then
  printf '{"text":"J","alt":"busy","class":"busy","tooltip":"JARVIS is working"}\n'
else
  printf '{"text":"J","alt":"idle","class":"idle","tooltip":"JARVIS — standing by\\nClick: open console\\nHold Super+\\` to speak"}\n'
fi
