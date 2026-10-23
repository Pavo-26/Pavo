#!/system/bin/sh
# Continuously sample Pixel 6 cumulative ODPM and cpufreq counters.

OUT="$1"
INTERVAL_S="${2:-1}"

if [ -z "$OUT" ]; then
    echo "usage: $0 OUTPUT_PATH [INTERVAL_SECONDS]" >&2
    exit 2
fi

: > "$OUT" || exit 1
chmod 0644 "$OUT" 2>/dev/null

SEQ=0
trap 'exit 0' TERM INT HUP

while true; do
    EPOCH_BEFORE_MS=$(date +%s%3N)
    {
        echo "@@SAMPLE_BEGIN $SEQ $EPOCH_BEFORE_MS"

        ODPM_BEFORE_MS=$(date +%s%3N)
        echo "@@ODPM_BEGIN $ODPM_BEFORE_MS"
        for f in /sys/bus/iio/devices/iio:device*/energy_value; do
            [ -r "$f" ] || continue
            echo "=== $f ==="
            cat "$f"
        done
        ODPM_AFTER_MS=$(date +%s%3N)
        echo "@@ODPM_END $ODPM_AFTER_MS"

        TIS_BEFORE_MS=$(date +%s%3N)
        echo "@@TIME_IN_STATE_BEGIN $TIS_BEFORE_MS"
        for p in /sys/devices/system/cpu/cpufreq/policy*; do
            f="$p/stats/time_in_state"
            [ -r "$f" ] || continue
            echo "=== $p ==="
            cat "$f"
        done
        TIS_AFTER_MS=$(date +%s%3N)
        echo "@@TIME_IN_STATE_END $TIS_AFTER_MS"

        EPOCH_AFTER_MS=$(date +%s%3N)
        echo "@@SAMPLE_END $SEQ $EPOCH_AFTER_MS"
    } >> "$OUT"
    SEQ=$((SEQ + 1))
    sleep "$INTERVAL_S"
done
