"""Pixel 6 zTT device utilities. See ../README.md."""

import subprocess
import json
import shlex
from pathlib import Path
import numpy as np
import os

import time
from typing import Optional

target_fps = 30
target_temp = 65
beta = 2

little_available_frequencies = [
    300000,
    574000,
    738000,
    930000,
    1098000,
    1197000,
    1328000,
    1401000,
    1598000,
    1704000,
    1803000,
]
mid_available_frequencies = [
    400000,
    553000,
    696000,
    799000,
    910000,
    1024000,
    1197000,
    1328000,
    1491000,
    1663000,
    1836000,
    1999000,
    2130000,
    2253000,
    2348000,
]
big_available_frequencies = [
    500000,
    851000,
    984000,
    1106000,
    1277000,
    1426000,
    1582000,
    1745000,
    1826000,
    2048000,
    2188000,
    2252000,
    2401000,
    2507000,
    2630000,
    2704000,
    2802000,
]
gpu_available_frequencies = [
    202000,
    251000,
    302000,
    351000,
    400000,
    471000,
    510000,
    572000,
    701000,
    762000,
    848000,
]

little_len = 11
mid_len = 15
big_len = 16
gpu_len = 11

little_min_freq = 300000
mid_min_freq = 400000
big_min_freq = 500000
gpu_min_freq = 202000

little_max_freq = 1803000
mid_max_freq = 2348000
big_max_freq = 2802000
gpu_max_freq = 848000

CPU_POLICIES = ("policy0", "policy4", "policy6")
_cpu_rate_limit_mode = None
_gpu_util_path = None
_last_temperatures = None
_target_window_cache = None
SCREEN_PREP_ENABLED = False  # The managed runner owns the screen.
TARGET_PACKAGE = os.environ.get("EARDVFS_TARGET_PACKAGE", "").strip()
PREFER_SURFACEVIEW = os.environ.get("EARDVFS_PREFER_SURFACEVIEW", "0") == "1"
ADB_RETRY_COUNT = 4
ADB_RETRY_SLEEP_SEC = 0.8
FPS_RETRY_COUNT = max(
    ADB_RETRY_COUNT,
    int(os.environ.get("EARDVFS_FPS_RETRY_COUNT", str(ADB_RETRY_COUNT))),
)
BATTERY_PREP_ENABLED = False  # Never spoof battery/charging state.


def _adb_shell_completed(
    command: str, timeout: int = 20, retries: int = ADB_RETRY_COUNT
) -> subprocess.CompletedProcess[str]:
    remote_command = shlex.quote(command)
    serial = os.environ.get("ANDROID_SERIAL", "").strip()
    if not serial:
        raise RuntimeError(
            "ANDROID_SERIAL is required; never select an implicit device"
        )
    last_result = None
    for attempt in range(retries):
        result = subprocess.run(
            [
                os.environ.get("EARDVFS_ADB", "adb"),
                "-s",
                serial,
                "shell",
                "sh",
                "-c",
                remote_command,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        last_result = result
        stderr = (result.stderr or "").strip().lower()
        if result.returncode == 0 and "error: closed" not in stderr:
            return result
        time.sleep(ADB_RETRY_SLEEP_SEC * (attempt + 1))
    if last_result is None:
        raise RuntimeError(f"adb shell never executed for command: {command}")
    raise RuntimeError("ADB command failed: " + (last_result.stderr or "").strip())


def _adb_shell(command: str, timeout: int = 20, retries: int = ADB_RETRY_COUNT) -> str:
    return _adb_shell_completed(
        command, timeout=timeout, retries=retries
    ).stdout.strip()


def _adb_shell_lines(
    command: str, timeout: int = 20, retries: int = ADB_RETRY_COUNT
) -> list[str]:
    last_output = ""
    for attempt in range(retries):
        output = _adb_shell(command, timeout=timeout, retries=1)
        lines = [line.strip() for line in output.splitlines()]
        if lines:
            return lines
        last_output = output
        time.sleep(ADB_RETRY_SLEEP_SEC * (attempt + 1))
    raise RuntimeError(
        f"empty adb output for command: {command!r}, last_output={last_output!r}"
    )


def _adb_read_int(
    command: str, timeout: int = 20, retries: int = ADB_RETRY_COUNT
) -> int:
    last_exc = None
    for attempt in range(retries):
        try:
            lines = _adb_shell_lines(command, timeout=timeout, retries=1)
            return int(lines[0])
        except Exception as exc:
            last_exc = exc
            time.sleep(ADB_RETRY_SLEEP_SEC * (attempt + 1))
    raise RuntimeError(f"failed to parse int from adb command: {command}") from last_exc


def _path_exists(path: str) -> bool:
    return _adb_shell(f"if [ -e {path} ]; then echo 1; else echo 0; fi") == "1"


def _get_gpu_util_path() -> str:
    global _gpu_util_path
    if _gpu_util_path is None:
        for path in (
            "/sys/devices/platform/28000000.mali/utilization",
            "/sys/devices/platform/1c500000.mali/utilization",
        ):
            if _path_exists(path):
                _gpu_util_path = path
                break
        if _gpu_util_path is None:
            raise FileNotFoundError("No supported Mali utilization node found")
    return _gpu_util_path


def rate_limit_paths():
    paths = []
    for policy in CPU_POLICIES:
        root = f"/sys/devices/system/cpu/cpufreq/{policy}"
        governor = _adb_shell("cat " + root + "/scaling_governor")
        if governor not in ("schedutil", "sched_pixel"):
            raise RuntimeError("Unsupported zTT governor: " + governor)
        base = root + "/" + governor
        if _path_exists(base + "/up_rate_limit_us") and _path_exists(
            base + "/down_rate_limit_us"
        ):
            paths += [base + "/up_rate_limit_us", base + "/down_rate_limit_us"]
        elif _path_exists(base + "/rate_limit_us"):
            paths += [base + "/rate_limit_us"]
        else:
            raise FileNotFoundError("No supported CPU rate-limit nodes: " + base)
    return paths


def _write_sysfs(path: str, value: int, *, verify: bool = True) -> None:
    _adb_shell(f"printf '%s' {int(value)} > {shlex.quote(path)}")
    if verify and _adb_shell("cat " + shlex.quote(path)) != str(int(value)):
        raise RuntimeError("Sysfs write/readback mismatch: " + path)


def _set_cpu_rate_limits(up: int, down: int) -> None:
    for path in rate_limit_paths():
        value = up if path.endswith("/up_rate_limit_us") else down
        if path.endswith("/rate_limit_us"):
            value = min(up, down)
        _write_sysfs(path, value)


def _set_gpu_dvfs_period(dvfs_period: int) -> None:
    _write_sysfs("/sys/class/misc/mali0/device/dvfs_period", dvfs_period)


def _parse_energy_snapshot(lines, start_idx: int):
    timestamp = int(lines[start_idx].split("=", 1)[1])
    idx = start_idx + 1
    channels = {}
    while idx < len(lines) and lines[idx].startswith("CH"):
        label = lines[idx].split("[", 1)[1].split("]", 1)[0]
        value = int(lines[idx].rsplit(",", 1)[1].strip())
        channels[label] = value
        idx += 1
    return timestamp, channels, idx


def _extract_energy_values(cpu_channels, gpu_channels):
    combined = {}
    combined.update(gpu_channels)
    combined.update(cpu_channels)
    little_e = combined["S4M_VDD_CPUCL0"]
    mid_e = combined["S3M_VDD_CPUCL1"]
    big_e = combined["S2M_VDD_CPUCL2"]
    gpu_e = combined["S2S_VDD_G3D"]
    return little_e, mid_e, big_e, gpu_e


def _parse_latency_fps(lines) -> float:
    frame_rows = []
    for line in lines:
        parts = [part.strip() for part in line.split("\t") if part.strip()]
        if len(parts) < 3:
            continue
        try:
            frame_rows.append(tuple(int(part) for part in parts[:3]))
        except ValueError:
            continue

    if len(frame_rows) < 2:
        return 0.0

    start_time = frame_rows[0][0]
    last_time = frame_rows[-1][-1]
    if last_time <= start_time:
        return 0.0
    return len(frame_rows) / ((last_time - start_time) / 1000000000)


def _normalize_surfaceflinger_layer(raw: str) -> str:
    name = raw.strip().split("Layer ", 1)[-1].strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name.startswith("RequestedLayerState{"):
        name = name[len("RequestedLayerState{") :]
        for marker in (" parentId=", " relativeParentId=", " z="):
            if marker in name:
                name = name.split(marker, 1)[0]
        name = name.rstrip("}").strip()
    return name


def _get_target_package_window(force_refresh: bool = False) -> Optional[str]:
    global _target_window_cache

    if not TARGET_PACKAGE:
        return None
    if _target_window_cache and not force_refresh:
        return _target_window_cache

    candidates = []
    for command, require_top_level in (
        ("dumpsys SurfaceFlinger", True),
        ("dumpsys SurfaceFlinger --list", False),
    ):
        try:
            lines = _adb_shell_lines(command, timeout=30)
        except Exception:
            continue
        for raw in lines:
            stripped = raw.strip()
            if require_top_level and not stripped.startswith("Layer ["):
                continue
            if not require_top_level and stripped.startswith("RequestedLayerState{"):
                continue
            name = _normalize_surfaceflinger_layer(raw)
            if TARGET_PACKAGE not in name:
                continue
            if name.startswith("["):
                continue
            if any(
                token in name
                for token in (
                    "ActivityRecordInputSink",
                    "ActivityRecord{",
                    "Background",
                    "Letterbox",
                    "SnapshotStartingWindow",
                    "StartingWindow",
                    "animation-leash",
                )
            ):
                continue
            # Douyin keeps its live video SurfaceView under SplashActivity.
            if "SplashActivity" in name and "SurfaceView" not in name:
                continue
            candidates.append(name)
        if candidates:
            break

    if not candidates:
        return None

    def priority(name: str):
        surface_penalty = 0 if PREFER_SURFACEVIEW and "SurfaceView" in name else 1
        blast_penalty = 0 if "BLAST" in name else 1
        activity_penalty = 0 if "/" in name else 1
        return surface_penalty, blast_penalty, activity_penalty, len(name)

    ordered = sorted(set(candidates), key=priority)
    validation_order = ordered
    if PREFER_SURFACEVIEW:
        surface_candidates = [name for name in ordered if "SurfaceView" in name]
        if surface_candidates:
            validation_order = surface_candidates

    for candidate in validation_order:
        try:
            latency = _adb_shell_lines(
                "dumpsys SurfaceFlinger --latency " + shlex.quote(candidate),
                timeout=30,
            )
        except Exception:
            continue
        if _parse_latency_fps(latency) > 0:
            _target_window_cache = candidate
            return _target_window_cache

    # Keep the preferred layer type so the caller retries instead of silently
    # switching from app content FPS to a composition-layer FPS.
    _target_window_cache = validation_order[0]
    return _target_window_cache


def get_states2(
    window,
    qos_type: str,
    qos_time_prev: float,
    byte_prev: Optional[int],
    packet_prev: Optional[int],
):
    """temps"""
    active_window = _get_target_package_window() or window
    last_exc = None
    retry_count = (
        FPS_RETRY_COUNT if qos_type == "fps" and TARGET_PACKAGE else ADB_RETRY_COUNT
    )
    for attempt in range(retry_count):
        try:
            if attempt and TARGET_PACKAGE:
                active_window = (
                    _get_target_package_window(force_refresh=True) or active_window
                )
            msg = (
                "cat /dev/thermal/tz-by-name/BIG/temp /dev/thermal/tz-by-name/MID/temp "
                "/dev/thermal/tz-by-name/LITTLE/temp /dev/thermal/tz-by-name/G3D/temp "
                "/dev/thermal/tz-by-name/qi_therm/temp /dev/thermal/tz-by-name/battery/temp"
                " && cat /dev/thermal/cdev-by-name/thermal-cpufreq-0/cur_state "
                "/dev/thermal/cdev-by-name/thermal-cpufreq-1/cur_state "
                "/dev/thermal/cdev-by-name/thermal-cpufreq-2/cur_state "
                "/dev/thermal/cdev-by-name/thermal-gpufreq-0/cur_state"
                " && cat /sys/bus/iio/devices/iio:device1/energy_value"
                " && cat /sys/bus/iio/devices/iio:device0/energy_value"
                " && cat /proc/stat"
                " && cat "
                + _get_gpu_util_path()
                + " && cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq "
                "/sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq "
                "/sys/devices/system/cpu/cpufreq/policy6/scaling_cur_freq "
                "/sys/class/misc/mali0/device/cur_freq"
                " && dumpsys SurfaceFlinger --latency " + shlex.quote(active_window)
            )
            result = _adb_shell_lines(msg, timeout=25, retries=1)
            if len(result) < 20:
                raise RuntimeError(f"too few lines from get_states2: {len(result)}")

            big_temp = int(result[0])
            mid_temp = int(result[1])
            little_temp = int(result[2])
            gpu_temp = int(result[3])
            qi_temp = int(result[4])
            battery_temp = int(result[5])

            little_cdev = int(result[6])
            mid_cdev = int(result[7])
            big_cdev = int(result[8])
            gpu_cdev = int(result[9])

            t2, gpu_channels, energy_idx = _parse_energy_snapshot(result, 10)
            t1, cpu_channels, energy_idx = _parse_energy_snapshot(result, energy_idx)
            little_e, mid_e, big_e, gpu_e = _extract_energy_values(
                cpu_channels, gpu_channels
            )

            li = []
            cpu_total_idx = next(
                i
                for i in range(energy_idx, len(result))
                if result[i].startswith("cpu ")
            )
            for i in result[cpu_total_idx + 1 : cpu_total_idx + 9]:
                temp = np.array(i.split()[1:], dtype=np.int64)
                li.append([temp[0:7].sum(), temp[3]])

            softirq_idx = next(
                i
                for i in range(cpu_total_idx + 9, len(result))
                if result[i].startswith("softirq ")
            )
            gpu_util_idx = softirq_idx + 1
            gpu_util = [int(result[gpu_util_idx]) / 100]

            little = int(result[gpu_util_idx + 1])
            mid = int(result[gpu_util_idx + 2])
            big = int(result[gpu_util_idx + 3])
            gpu = int(result[gpu_util_idx + 4])

            freqs = np.array([little, mid, big, gpu])

            byte_cur = None
            packet_cur = None
            qos_time_cur = None
            match qos_type:
                case "fps":
                    qos = _parse_latency_fps(result[gpu_util_idx + 5 :])
                    if TARGET_PACKAGE and qos <= 0:
                        raise RuntimeError(
                            f"no FPS samples for target window={active_window}"
                        )
                case "byte":
                    byte_cur = get_packet_info(active_window, qos_type)
                    qos_time_cur = time.time()
                    qos = cal_packet(
                        (byte_prev, byte_cur), (qos_time_prev, qos_time_cur)
                    )
                    print(
                        byte_cur[1] - byte_prev[1],
                        byte_cur[0] - byte_prev[0],
                        qos_time_cur - qos_time_prev,
                        qos,
                    )
                case "packet":
                    packet_cur = get_packet_info(active_window, qos_type)
                    qos_time_cur = time.time()
                    qos = cal_packet(
                        (packet_prev, packet_cur), (qos_time_prev, qos_time_cur)
                    )

            c_states = [little_cdev, mid_cdev, big_cdev, gpu_cdev]
            temps = [
                little_temp / 1000,
                mid_temp / 1000,
                big_temp / 1000,
                gpu_temp / 1000,
                qi_temp / 1000,
                battery_temp / 1000,
            ]

            return (
                c_states,
                temps,
                qos,
                t1,
                t2,
                little_e,
                mid_e,
                big_e,
                gpu_e,
                np.array(li),
                gpu_util,
                freqs,
                qos_time_cur,
                byte_cur,
                packet_cur,
            )
        except Exception as exc:
            last_exc = exc
            time.sleep(min(ADB_RETRY_SLEEP_SEC * (attempt + 1), 3.2))
            continue

    raise RuntimeError(f"get_states2 failed for window={active_window}") from last_exc


def get_core_util():
    result = _adb_shell_lines("cat /proc/stat")

    li = []
    for i in result[1:9]:
        temp = np.array(i.split()[1:], dtype=np.int32)
        li.append([temp[0:7].sum(), temp[3]])

    return np.array(li)


def cal_core_util(b, a):
    x = b - a
    return 1 - x[:, 1] / x[:, 0]


def get_cpu_util():
    result = _adb_shell_lines("top -n 1")
    cpu_util = [int(result[3].split("%idle")[0].split(" ")[-1]) / 800 * 100]
    return cpu_util


def get_gpu_util():
    result = _adb_shell_lines("cat " + _get_gpu_util_path())
    gpu_util = [int(result[0]) / 100]
    return gpu_util


def get_frequency():
    result = _adb_shell_lines(
        "cat /sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq "
        "/sys/devices/system/cpu/cpufreq/policy4/scaling_cur_freq "
        "/sys/devices/system/cpu/cpufreq/policy6/scaling_cur_freq "
        "/sys/class/misc/mali0/device/cur_freq"
    )
    little = int(result[0])
    mid = int(result[1])
    big = int(result[2])
    gpu = int(result[3])

    return little, mid, big, gpu


def get_window():
    target_window = _get_target_package_window(force_refresh=True)
    if target_window:
        print(f"target SurfaceFlinger layer: {target_window}")
        return target_window

    result = _adb_shell_lines("dumpsys SurfaceFlinger", timeout=30)
    ans = "asdf"
    for i in range(len(result)):
        if "Fingerprint" in result[i]:
            for candidate in result[i + 1 : i + 6]:
                candidate = candidate.strip()
                if not candidate.startswith("Layer ["):
                    continue
                print(candidate)
                if "Letterbox" in candidate:
                    continue
                if "Background" in candidate:
                    continue
                ans = candidate
                break
            if ans != "asdf":
                break
    ans = ans.split("Layer ", 1)[-1].strip()
    if ans.startswith("[") and ans.endswith("]"):
        ans = ans[1:-1]

    return ans


def get_fps(window):
    active_window = _get_target_package_window() or window
    result = _adb_shell_lines(
        "dumpsys SurfaceFlinger --latency " + shlex.quote(active_window), timeout=30
    )
    fps = _parse_latency_fps(result)

    if TARGET_PACKAGE and fps <= 0:
        active_window = _get_target_package_window(force_refresh=True) or active_window
        result = _adb_shell_lines(
            "dumpsys SurfaceFlinger --latency " + shlex.quote(active_window), timeout=30
        )
        fps = _parse_latency_fps(result)

    return fps


def get_energy():
    result = _adb_shell_lines("cat /sys/bus/iio/devices/iio:device0/energy_value")
    result2 = _adb_shell_lines("cat /sys/bus/iio/devices/iio:device1/energy_value")

    t1, cpu_channels, _ = _parse_energy_snapshot(result, 0)
    t2, gpu_channels, _ = _parse_energy_snapshot(result2, 0)
    little, mid, big, gpu = _extract_energy_values(cpu_channels, gpu_channels)

    return t1, t2, little, mid, big, gpu


def get_battery_power():
    current = _adb_read_int("cat /sys/class/power_supply/battery/current_now")
    voltage = _adb_read_int("cat /sys/class/power_supply/battery/voltage_now")
    current = current * 1.0 / 1e3
    voltage = voltage * 1.0 / 1e6
    return [float(abs(current) * abs(voltage))]  # mW


def get_temperatures():
    global _last_temperatures

    command = (
        "cat /dev/thermal/tz-by-name/BIG/temp /dev/thermal/tz-by-name/MID/temp "
        "/dev/thermal/tz-by-name/LITTLE/temp /dev/thermal/tz-by-name/G3D/temp "
        "/dev/thermal/tz-by-name/qi_therm/temp /dev/thermal/tz-by-name/battery/temp"
    )

    try:
        result = _adb_shell_lines(command, timeout=25, retries=max(ADB_RETRY_COUNT, 6))
        big = int(result[0])
        mid = int(result[1])
        little = int(result[2])
        gpu = int(result[3])
        qi = int(result[4])
        battery = int(result[5])
        _last_temperatures = (
            little / 1000,
            mid / 1000,
            big / 1000,
            gpu / 1000,
            qi / 1000,
            battery / 1000,
        )
        return _last_temperatures
    except Exception:
        if _last_temperatures is not None:
            return _last_temperatures
        raise


_saved_controls = {}
_snapshot_path = None


def _frequency_roots():
    return [f"/sys/devices/system/cpu/cpufreq/{policy}" for policy in CPU_POLICIES] + [
        "/sys/class/misc/mali0/device"
    ]


def save_device_controls():
    global _saved_controls, _snapshot_path
    if _saved_controls:
        raise RuntimeError("A zTT control snapshot is already active")
    paths = [
        root + "/" + key
        for root in _frequency_roots()
        for key in ("scaling_min_freq", "scaling_max_freq")
    ]
    paths += rate_limit_paths() + ["/sys/class/misc/mali0/device/dvfs_period"]
    # Read EVERY value before allowing any control mutation.
    captured = {path: int(_adb_shell("cat " + shlex.quote(path))) for path in paths}
    _snapshot_path = Path(os.environ["EARDVFS_CONTROL_SNAPSHOT"])
    _snapshot_path.write_text(json.dumps(captured, indent=2) + "\n")
    _saved_controls.update(captured)


def _set_limits(root, minimum, maximum):
    minimum, maximum = int(minimum), int(maximum)
    if minimum > maximum:
        raise ValueError("Frequency minimum exceeds maximum")
    current_min = int(_adb_shell("cat " + root + "/scaling_min_freq"))
    if current_min > maximum:
        _write_sysfs(root + "/scaling_min_freq", minimum)
    _write_sysfs(root + "/scaling_max_freq", maximum)
    _write_sysfs(root + "/scaling_min_freq", minimum)


def set_frequency(
    little_min, little_max, mid_min, mid_max, big_min, big_max, gpu_min, gpu_max
):
    if not _saved_controls:
        raise RuntimeError("Missing control snapshot")
    for root, bounds in zip(
        _frequency_roots(),
        (
            (little_min, little_max),
            (mid_min, mid_max),
            (big_min, big_max),
            (gpu_min, gpu_max),
        ),
    ):
        # Retain the original min-then-max requests; cpufreq may clamp them.
        _write_sysfs(root + "/scaling_min_freq", bounds[0], verify=False)
        _write_sysfs(root + "/scaling_max_freq", bounds[1], verify=False)


def unset_frequency():
    if not _saved_controls:
        raise RuntimeError("Missing control snapshot")
    for root in _frequency_roots():
        _set_limits(
            root,
            _saved_controls[root + "/scaling_min_freq"],
            _saved_controls[root + "/scaling_max_freq"],
        )


def set_rate_limit_us(rate_limit_us, dvfs_period):
    if not _saved_controls:
        raise RuntimeError("Missing control snapshot")
    _set_cpu_rate_limits(rate_limit_us, rate_limit_us)
    _set_gpu_dvfs_period(dvfs_period)


def unset_rate_limit_us():
    if not _saved_controls:
        raise RuntimeError("Missing control snapshot")
    for path, value in _saved_controls.items():
        if not path.endswith(("/scaling_min_freq", "/scaling_max_freq")):
            _write_sysfs(path, value)


def restore_device_controls():
    errors = []
    for root in _frequency_roots():
        if root + "/scaling_min_freq" in _saved_controls:
            try:
                _set_limits(
                    root,
                    _saved_controls[root + "/scaling_min_freq"],
                    _saved_controls[root + "/scaling_max_freq"],
                )
            except Exception as exc:
                errors.append(str(exc))
    for path, value in _saved_controls.items():
        if not path.endswith(("/scaling_min_freq", "/scaling_max_freq")):
            try:
                _write_sysfs(path, value)
            except Exception as exc:
                errors.append(str(exc))
    if _snapshot_path is not None:
        _snapshot_path.with_suffix(".restore.json").write_text(
            json.dumps({"restored": not errors, "errors": errors}, indent=2) + "\n"
        )
    if errors:
        raise RuntimeError("zTT control restore failed: " + "; ".join(errors))


def set_root():
    if _adb_shell("id -u") != "0":
        raise RuntimeError(
            "An existing root adbd shell is required; this baseline never changes root/SELinux"
        )


def set_brightness(level):
    """No-op: brightness is owned and restored by the enclosing runner."""


def turn_on_screen():
    """No-op: the enclosing runner owns the foreground scene."""


def turn_off_screen():
    """No-op: never lock a managed experiment's screen."""


def turn_off_usb_charging():
    """No-op: never spoof Android's charging state."""


def turn_on_usb_charging():
    """No-op: never spoof Android's charging state."""


def get_pid(window):
    if not TARGET_PACKAGE:
        raise ValueError("Explicit target package is required")
    return int(_adb_shell("pidof -s " + shlex.quote(TARGET_PACKAGE)))


def get_packet_info(window, target):
    # Online control input; the managed runner permits FPS QoS only.
    lines = _adb_shell_lines(f"cat /proc/{get_pid(window)}/net/dev")
    columns = next(line.split() for line in lines if "wlan0" in line)
    indices = (1, 9) if target == "byte" else (2, 10)
    return tuple(int(columns[i]) for i in indices)


def cal_packet(values, timestamps):
    return sum(b - a for a, b in zip(*values)) / (
        (timestamps[1] - timestamps[0]) * 1000
    )
