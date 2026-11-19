#!/usr/bin/env sh
set -eu
output=${1:-storage/samples/clipforge-sample.mp4}
mkdir -p "$(dirname "$output")"
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
narration='The first five seconds of a video answer one question: why should I keep watching? Start with a specific problem your audience recognizes. Then give them one thing they can try today. For example, before filming an interview, write down the smallest useful lesson you can explain in twenty seconds. Ask a question that gets your guest to tell that story. The answer will make a much stronger clip because the viewer gets a complete idea.'
if command -v say >/dev/null 2>&1; then
  say -v Samantha -r 146 -o "$temp/speech.aiff" "$narration"
elif command -v espeak-ng >/dev/null 2>&1; then
  espeak-ng -s 145 -w "$temp/speech.aiff" "$narration"
else
  printf '%s\n' 'Install espeak-ng or use macOS say to generate a narration.' >&2
  exit 1
fi
ffmpeg -v error -f lavfi -i 'testsrc2=size=640x360:rate=30' -i "$temp/speech.aiff" \
  -shortest -c:v libx264 -pix_fmt yuv420p -c:a aac -movflags +faststart "$output"
