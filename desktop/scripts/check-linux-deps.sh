#!/usr/bin/env bash
# Verify GStreamer plugins required by WebKitGTK for mic capture and TTS playback.
set -euo pipefail

if [[ "$(uname -s)" != "Linux" ]]; then
  exit 0
fi

if ! command -v gst-inspect-1.0 &>/dev/null; then
  echo "JARVIS desktop: gstreamer is not installed (WebKitGTK needs it for voice I/O)." >&2
  echo "  Arch:   sudo pacman -S gstreamer gst-plugins-base gst-plugins-good pipewire-pulse" >&2
  echo "  Debian: sudo apt install gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good" >&2
  echo "  Fedora: sudo dnf install gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good" >&2
  exit 1
fi

missing=()
for element in autoaudiosink pulsesink pulsesrc; do
  if ! gst-inspect-1.0 "$element" &>/dev/null; then
    missing+=("$element")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  echo "JARVIS desktop: missing GStreamer plugins: ${missing[*]}" >&2
  echo "Voice push-to-talk and spoken replies will not work until these are installed." >&2
  echo "  Arch:   sudo pacman -S gst-plugins-good pipewire-pulse" >&2
  echo "  Debian: sudo apt install gstreamer1.0-plugins-good pulseaudio" >&2
  echo "  Fedora: sudo dnf install gstreamer1-plugins-good pulseaudio" >&2
  exit 1
fi

exit 0
