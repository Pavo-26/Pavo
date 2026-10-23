"""Fixed main-experiment parameters and configuration checks."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

APPS = {
    "dy": ("Douyin", "com.ss.android.ugc.aweme", "抖音"),
    "ks": ("Kuaishou", "com.smile.gifmaker", "快手"),
    "aqy": ("iQIYI", "com.qiyi.video", "爱奇艺"),
    "tb": ("Taobao", "com.taobao.taobao", "淘宝"),
    "wb": ("Weibo", "com.sina.weibo", "微博"),
}
POLICIES = {
    "pixel6": ("android_default", "performance", "scx_simple", "scx_lavd",
               "uclamp_gfx_700", "ztt", "pavo"),
    "oneplus15": ("stock", "stock_schedutil", "pavo"),
}
DURATION_S = 600
REPETITIONS = 5
WORKERS = 16
PERFDOG_VERSION = "11.2.250408"
APP_ORDER = ("dy", "aqy", "tb", "wb", "ks")
# Bound host scheduling/GUI stop latency; never extend capture to finish a round.
RECORDING_STOP_TOLERANCE_S = 1.0
INTER_RUN_DELAY_S = 10
MIN_BATTERY_PCT = 25
# Allow background preparation and cleanup around the recording window.
BACKGROUND_STARTUP_ALLOWANCE_S = 120
BACKGROUND_CLEANUP_ALLOWANCE_S = 60
BACKGROUND_TIMEOUT_S = (DURATION_S + BACKGROUND_STARTUP_ALLOWANCE_S
                        + BACKGROUND_CLEANUP_ALLOWANCE_S)


class CleanupError(RuntimeError):
    """Device state restoration failed."""


def finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label}: expected a finite number")
    return float(value)


def select(raw: str, choices: Any) -> list[str]:
    values = list(choices) if raw == "all" else raw.split(",")
    if not values or len(values) != len(set(values)) or any(v not in choices for v in values):
        raise ValueError(f"Invalid selection {raw!r}; expected {list(choices)}")
    return values



def validate_runtime(config: dict[str, Any], platform: str,
                     policies: list[str], apps: list[str], *, ztt_training: bool = False) -> None:
    """Check the actual environment, tool versions and configuration paths."""
    if config.get("schema_version") != 1 or config.get("platform") != platform:
        raise ValueError("Runtime configuration has the wrong schema/platform")
    perf = config["perfdog"]
    if perf.get("version") != PERFDOG_VERSION:
        raise ValueError("Both devices require PerfDog " + PERFDOG_VERSION)
    if not perf.get("executable") or not perf.get("data_root"):
        raise ValueError("Specify the actual PerfDog executable and export root")
    if not perf.get("ui_config"):
        raise ValueError("Provide the PerfDog window and click configuration")
    env = config["environment"]
    if env.get("brightness_percent") != 50:
        raise ValueError("Set brightness to 50%")
    if not isinstance(env.get("brightness_raw"), int) or env["brightness_raw"] < 0:
        raise ValueError("brightness_raw must be a nonnegative integer")
    if not env.get("fingerprint") or not env.get("kernel_release"):
        raise ValueError("Specify the device fingerprint and kernel release")
    release = env["kernel_release"]
    if platform == "pixel6":
        if not release.startswith("6.12."):
            raise ValueError("Pixel requires common 6.12")
        debian_version = env.get("debian_version")
        if (not isinstance(debian_version, str)
                or not re.fullmatch(r"12\.[0-9]+", debian_version)):
            raise ValueError("Set debian_version to the installed Debian 12.x release")
    elif not release.startswith("6.12.23") or env.get("kernel_kind") != "control":
        raise ValueError("OnePlus requires a 6.12.23 kernel and kernel_kind='control'")
    for app in apps:
        if not config["apps"].get(app, {}).get("version_name"):
            raise ValueError(f"Specify the installed App version: {app}")
        if platform == "pixel6":
            record = config["apps"][app]
            if not record.get("ui_config"):
                raise ValueError("Provide the Pixel App configuration")
        if platform == "oneplus15":
            for key in ("device_config", "apps_config"):
                record = config["apps"][app]
                if not record.get(key):
                    raise ValueError(f"OnePlus App flow requires {key}: {app}")
    for policy in policies:
        if policy not in ("pavo", "scx_simple", "scx_lavd", "uclamp_gfx_700"):
            continue
        key = "uclamp_floor_daemon" if policy == "uclamp_gfx_700" else ("scx_pavo" if policy == "pavo" else policy)
        binary = config["binaries"][key]
        if Path(binary.get("path", "")).name != key:
            raise ValueError(f"Use the explicit {key} executable, not an app-specific alias")
        if binary.get("arguments", []):
            raise ValueError(f"{key}: the runner starts this executable without arguments")
        if key in ("scx_simple", "scx_lavd") and binary.get("upstream_version") != "1.0.9":
            raise ValueError(f"{key}: pin upstream v1.0.9")
    if "ztt" in policies:
        ztt = config["ztt"]
        if not ztt.get("source_dir") or not ztt.get("python"):
            raise ValueError("zTT requires explicit source and interpreter directories")
        if not ztt_training:
            if not ztt.get("model_dir") or not ztt.get("model_stem"):
                raise ValueError("Specify the zTT checkpoint directory and filename stem")


def validate_measurement(result: dict[str, Any]) -> None:
    """Check capture completion and timing."""
    if result.get("measurement_tool_version") != PERFDOG_VERSION:
        raise ValueError("PerfDog version must be " + PERFDOG_VERSION)
    duration = finite_number(result["host_recording_duration_s"], "recording duration")
    interaction = finite_number(result["host_interaction_duration_s"], "interaction duration")
    recorded_interaction = finite_number(
        result["recorded_interaction_duration_s"], "recorded interaction duration")
    stop_lag = finite_number(result["interaction_stop_lag_s"], "interaction stop lag")
    lead = finite_number(result["recording_lead_s"], "recording lead")
    tail = finite_number(result["recording_tail_s"], "recording tail")
    if not DURATION_S <= duration <= DURATION_S + RECORDING_STOP_TOLERANCE_S:
        raise ValueError(f"PerfDog recording missed the {DURATION_S}-second boundary: {duration:.3f}s")
    if (interaction <= 0 or recorded_interaction <= 0 or lead < 0 or stop_lag < 0
            or not 0 <= tail <= RECORDING_STOP_TOLERANCE_S):
        raise ValueError("App interaction did not cover the recording boundary")
    if (abs(duration - lead - recorded_interaction - tail) > 0.001
            or abs(interaction - recorded_interaction - stop_lag) > 0.001):
        raise ValueError("Recording and interaction timestamps are inconsistent")
    if not result.get("stopped_at_boundary"):
        raise ValueError("PerfDog recording was not stopped at the boundary")
    if not result.get("capture_complete"):
        raise ValueError("Capture did not complete")
    if not result.get("copied_xlsx"):
        raise ValueError("Raw export location is missing")
