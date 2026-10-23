#!/bin/bash
# Run two encoders inside ADEB.
set -euo pipefail
if [ "$#" -ne 3 ]; then
    echo "Usage: bash run.sh INPUT_1080P_VIDEO NEW_OUTPUT_DIR MEDIA_SECONDS" >&2
    exit 2
fi
input=$1
out=$2
seconds=$3
[[ "$seconds" =~ ^[1-9][0-9]*$ ]] || exit 2
test -f "$input"
command -v ffmpeg >/dev/null
test ! -e "$out"
mkdir -p "$out"
pids=()
stop_children() {
    for pid in "${pids[@]}"; do kill -INT "$pid" 2>/dev/null || true; done
    for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
    exit 130
}
trap stop_children INT TERM HUP
date -u > "$out/started.txt"
ffmpeg -version > "$out/version.txt"
for i in 1 2; do
    ffmpeg -y -nostdin -hide_banner \
        -stats_period 1 -progress "$out/progress_${i}.txt" \
        -stream_loop -1 -i "$input" -t "$seconds" \
        -vf scale=1280:-2 \
        -c:v libx264 -preset medium -crf 28 -threads 6 \
        -c:a aac -b:a 128k "$out/output_${i}.mp4" \
        > "$out/stdout_${i}.log" 2> "$out/stderr_${i}.log" &
    pids+=("$!")
done
status=0
for i in 0 1; do
    rc=0
    wait "${pids[$i]}" || rc=$?
    printf '%s\n' "$rc" > "$out/exit_$((i + 1)).txt"
    if [ "$rc" -ne 0 ]; then status=1; fi
done
date -u > "$out/finished.txt"
exit "$status"
