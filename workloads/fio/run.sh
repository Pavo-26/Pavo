#!/system/bin/sh
# Android-native random read/write workload.
set -eu
if [ "$#" -ne 3 ]; then
    echo "Usage: sh run.sh FIO_BINARY NEW_OUTPUT_DIR RUNTIME_SECONDS" >&2
    exit 2
fi
fio=$1
out=$2
seconds=$3
case "$seconds" in ''|*[!0-9]*|0) exit 2 ;; esac
test -x "$fio"
test ! -e "$out"
config=$(dirname "$0")/randrw.fio
test -r "$config"
mkdir -p "$out"
"$fio" --version > "$out/version.txt"
date -u > "$out/started.txt"
rc=0
"$fio" "$config" --runtime="$seconds" --filename="$out/fio_testfile.dat" \
    --write_bw_log="$out/fio_bw" --write_iops_log="$out/fio_iops" \
    --write_lat_log="$out/fio_lat" --output-format=json \
    > "$out/fio.json" 2> "$out/stderr.log" || rc=$?
printf '%s\n' "$rc" > "$out/exit.txt"
date -u > "$out/finished.txt"
exit "$rc"
