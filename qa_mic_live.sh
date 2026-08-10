#!/usr/bin/env bash
# Human-in-the-loop microphone diagnostic: permissions, route, then real signal.
set -u
cd "$(dirname "$0")"

failures=0
echo "Deckbridge action preflight"
if ! ./mic_key.sh --check; then
  failures=$((failures + 1))
fi

default_input="$(system_profiler SPAudioDataType 2>/dev/null | awk '
  /^        [^ ].*:$/ { device=$0; sub(/^        /, "", device); sub(/:$/, "", device) }
  /Default Input Device: Yes/ { print device; exit }
')"
if [ -n "$default_input" ]; then
  echo "default_input=$default_input"
else
  echo "MIC_ROUTE=FAIL no default input device"
  failures=$((failures + 1))
fi

ffmpeg_bin="$(command -v ffmpeg 2>/dev/null || true)"
if [ -z "$ffmpeg_bin" ]; then
  echo "MIC_AUDIO=SKIP install ffmpeg to run the live signal test"
  exit "$failures"
fi

devices="$($ffmpeg_bin -hide_banner -f avfoundation -list_devices true -i '' 2>&1 || true)"
device_index="$(printf '%s\n' "$devices" | awk -v wanted="$default_input" '
  /AVFoundation audio devices:/ { audio=1; next }
  audio && index($0, "] " wanted) {
    line=$0
    sub(/^.*\[/, "", line)
    sub(/\].*$/, "", line)
    print line
    exit
  }
')"
if [ -z "$device_index" ]; then
  echo "MIC_AUDIO=FAIL default input is absent from AVFoundation"
  exit 1
fi

sample="$(mktemp "${TMPDIR:-/tmp}/deckbridge-mic.XXXXXX.wav")" || exit 1
trap 'rm -f "$sample"' EXIT
echo "Speak normally for three seconds now…" >&2
if ! "$ffmpeg_bin" -hide_banner -loglevel error -f avfoundation \
    -i ":$device_index" -t 3 -ac 1 -ar 16000 -y "$sample"; then
  echo "MIC_AUDIO=FAIL capture denied; enable your terminal under Privacy & Security > Microphone"
  exit 1
fi
levels="$($ffmpeg_bin -hide_banner -i "$sample" -af volumedetect -f null - 2>&1)"
max_volume="$(printf '%s\n' "$levels" | sed -n 's/.*max_volume: \([-0-9.]*\) dB.*/\1/p' | tail -n 1)"
if [ -z "$max_volume" ]; then
  echo "MIC_AUDIO=FAIL capture contained no measurable samples"
  exit 1
fi
if awk -v level="$max_volume" 'BEGIN { exit !(level > -50) }'; then
  echo "MIC_AUDIO=PASS max_volume=${max_volume}dB"
else
  echo "MIC_AUDIO=FAIL max_volume=${max_volume}dB; check the selected input and AirPods connection"
  failures=$((failures + 1))
fi

if [ "$failures" -eq 0 ]; then
  echo "MIC_READY=yes"
else
  echo "MIC_READY=no"
fi
exit "$failures"
