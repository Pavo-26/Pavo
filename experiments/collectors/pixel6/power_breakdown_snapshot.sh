#!/system/bin/sh
# Pixel 6 power counters and device-state snapshot.

LABEL="$1"

source_header() {
    echo "@@SOURCE $1@@"
}

capture_odpm() {
    source_header odpm
    found=0
    for f in /sys/bus/iio/devices/iio:device*/energy_value; do
        [ -r "$f" ] || continue
        echo "=== $f ==="
        cat "$f" || return 1
        found=1
    done
    [ "$found" = 1 ]
}

capture_device_time() {
    source_header device_time
    date +%s%3N
}

capture_cpu_time_in_state() {
    source_header time_in_state
    for p in /sys/devices/system/cpu/cpufreq/policy*; do
        f="$p/stats/time_in_state"
        [ -r "$f" ] || continue
        echo "=== $p ==="
        cat "$f"
    done
}

capture_proc_stat() {
    source_header proc_stat
    cat /proc/stat
}

capture_cpuidle() {
    source_header cpuidle
    for c in /sys/devices/system/cpu/cpu[0-9]*; do
        cpu=$(basename "$c")
        for s in "$c"/cpuidle/state*; do
            [ -d "$s" ] || continue
            state=$(basename "$s")
            name=$(cat "$s/name" 2>/dev/null)
            idle_time=$(cat "$s/time" 2>/dev/null)
            usage=$(cat "$s/usage" 2>/dev/null)
            echo "$cpu|$state|$name|$idle_time|$usage"
        done
    done
}

capture_thermal() {
    source_header thermal
    for z in /sys/class/thermal/thermal_zone*; do
        [ -d "$z" ] || continue
        zone_type=$(cat "$z/type" 2>/dev/null)
        temp=$(cat "$z/temp" 2>/dev/null)
        echo "$z|$zone_type|$temp"
    done
}

capture_battery() {
    source_header battery
    for key in current_now voltage_now temp charge_counter capacity status; do
        f="/sys/class/power_supply/battery/$key"
        [ -r "$f" ] || continue
        echo "$key=$(cat "$f" 2>/dev/null)"
    done
}

capture_gpu() {
    source_header gpu
    gpu_dir=/sys/devices/platform/1c500000.mali
    for key in cur_freq utilization; do
        f="$gpu_dir/$key"
        [ -r "$f" ] || continue
        echo "$key=$(cat "$f" 2>/dev/null)"
    done
}

capture_gpu_time_in_state() {
    source_header gpu_time_in_state
    f=/sys/devices/platform/1c500000.mali/time_in_state
    if [ -r "$f" ]; then
        echo "=== gpu ==="
        cat "$f"
    fi
}

capture_devfreq_time_in_state() {
    source_header devfreq_time_in_state
    for d in /sys/class/devfreq/*; do
        f="$d/time_in_state"
        [ -r "$f" ] || continue
        echo "=== $d ==="
        cat "$f"
    done
}

if [ "$LABEL" = "sample" ]; then
    # Capture timestamps around the ODPM read.
    capture_device_time
    capture_odpm || exit 1
    capture_device_time
elif [ "$LABEL" = "end" ]; then
    # Stop the primary ODPM interval first, then collect supporting context.
    capture_device_time
    capture_odpm || exit 1
    capture_cpu_time_in_state
    capture_proc_stat
    capture_cpuidle
    capture_gpu_time_in_state
    capture_devfreq_time_in_state
    capture_gpu
    capture_battery
    capture_thermal
else
    # Capture supporting context first and start the primary ODPM interval last.
    capture_thermal
    capture_battery
    capture_gpu
    capture_devfreq_time_in_state
    capture_gpu_time_in_state
    capture_cpuidle
    capture_proc_stat
    capture_cpu_time_in_state
    capture_odpm || exit 1
    capture_device_time
fi
