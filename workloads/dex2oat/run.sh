#!/system/bin/sh
# Compile the selected WeChat version through Android package manager.
set -eu
if [ "$#" -ne 1 ]; then
    echo "Usage: sh run.sh NEW_OUTPUT_DIR" >&2
    exit 2
fi
out=$1
pkg=com.tencent.mm
test ! -e "$out"
test "$(id -u)" = 0
if pidof dex2oat64 dex2oat >/dev/null 2>&1; then
    echo "Another dex2oat job is already running" >&2
    exit 1
fi
details=$(dumpsys package "$pkg")
printf '%s\n' "$details" | grep -q 'versionName=8.0.64'
printf '%s\n' "$details" | grep -Eq 'versionCode=2940([[:space:]]|$)'
mkdir -p "$out"
printf '%s\n' "$details" > "$out/package.txt"
cmd package compile --reset "$pkg" > "$out/reset.stdout.log" 2> "$out/reset.stderr.log"
grep -q Success "$out/reset.stdout.log"
if pidof dex2oat64 dex2oat >/dev/null 2>&1; then
    echo "Compilation remained active after reset" >&2
    exit 1
fi
date -u > "$out/started.txt"
cmd package compile -m speed -p PRIORITY_BACKGROUND -f "$pkg" \
    > "$out/compile.stdout.log" 2> "$out/compile.stderr.log" &
compiler=$!
(
    while kill -0 "$compiler" 2>/dev/null; do
        date -u
        for pid in $(pidof dex2oat64 dex2oat 2>/dev/null); do
            echo "PID $pid"
            for name in stat status cgroup cmdline; do
                echo "FILE $name"
                if [ "$name" = cmdline ]; then
                    tr '\000' ' ' < "/proc/$pid/$name" 2>/dev/null || true
                    echo
                else
                    cat "/proc/$pid/$name" 2>/dev/null || true
                fi
            done
        done
        sleep 1
    done
) > "$out/process_samples.log" 2> "$out/observer.stderr.log" &
observer=$!
rc=0
wait "$compiler" || rc=$?
wait "$observer" || true
printf '%s\n' "$rc" > "$out/exit.txt"
date -u > "$out/finished.txt"
exit "$rc"
