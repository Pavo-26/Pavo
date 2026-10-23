"""Capture selected Perfetto events."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent / "configs/scheduler.pbtxt"
EVENTS_QUERY = (
    "if [ -r /sys/kernel/tracing/available_events ]; then "
    "cat /sys/kernel/tracing/available_events; "
    "else cat /sys/kernel/debug/tracing/available_events; fi"
)


def inspect_config(text: str) -> tuple[int, list[str]]:
    clean = re.sub(r"#[^\n]*", "", text)
    durations = re.findall(r"\bduration_ms\s*:\s*(\d+)\b", clean)
    if len(durations) != 1 or not 1000 <= int(durations[0]) <= 600000:
        raise ValueError("Config requires one duration_ms between 1000 and 600000")
    events = sorted(set(re.findall(r'\bftrace_events\s*:\s*"([a-zA-Z0-9_]+/[a-zA-Z0-9_]+)"', clean)))
    if not events:
        raise ValueError("No explicit ftrace events found")
    return int(durations[0]), events


def configured_capture(path: Path, duration_s: int | None = None) -> tuple[str, int, list[str]]:
    text = path.read_text(encoding="utf-8-sig")
    inspect_config(text)
    if duration_s is not None:
        if type(duration_s) is not int or not 1 <= duration_s <= 600:
            raise ValueError("Trace duration must be 1..600 seconds")
        # Remove comments before overriding the single validated duration field.
        text = re.sub(r"#[^\n]*", "", text)
        text = re.sub(r"\bduration_ms\s*:\s*\d+\b",
                      f"duration_ms: {duration_s * 1000}", text)
    duration, events = inspect_config(text)
    return text, duration, events


def adb_run(adb: str, serial: str, command: list[str], *, input_text: str | None = None,
            timeout: float = 20) -> str:
    try:
        result = subprocess.run(
            [adb, "-s", serial, *command], input=input_text, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("ADB operation failed: " + type(exc).__name__) from None
    if result.returncode != 0:
        # Do not echo commands with serials or arbitrary device error contents.
        raise RuntimeError(f"ADB operation returned {result.returncode}")
    return result.stdout.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--duration", type=int, help="Override the config duration, 1..600 seconds")
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    if not args.serial.strip():
        parser.error("--serial must not be empty")
    try:
        config, duration, events = configured_capture(args.config, args.duration)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    capture_settings = {
        "execute": args.execute, "device_identifier": "omitted",
        "duration_ms": duration, "events": events,
        "config_role": (
            "Pixel 6 ADEB"
            if args.config.resolve() == DEFAULT_CONFIG.resolve() else "caller-supplied configuration"),
    }
    if args.output_dir.resolve().is_relative_to(Path(__file__).resolve().parents[3]):
        parser.error("Choose an output directory outside the source tree")
    if args.output_dir.exists():
        parser.error("--output-dir already exists; use a new directory")
    args.output_dir.mkdir(parents=True, exist_ok=False)
    remote = "/data/misc/perfetto-traces/pavo_" + uuid.uuid4().hex + ".pftrace"
    metadata = dict(capture_settings, started_utc=datetime.now(timezone.utc).isoformat(),
                    remote_trace=remote, status="preflight", remote_trace_retained=True)
    (args.output_dir / "config.pbtxt").write_text(config, encoding="utf-8")
    try:
        if adb_run(args.adb, args.serial, ["shell", "id -u"]) != "0":
            raise RuntimeError("Root adbd required")
        if not adb_run(args.adb, args.serial, ["shell", "command -v perfetto"]):
            raise RuntimeError("Perfetto is not available")
        available = set(adb_run(args.adb, args.serial, ["shell", EVENTS_QUERY]).split())
        missing = [event for event in events if event.replace("/", ":") not in available]
        if missing:
            raise RuntimeError("Missing ftrace events: " + ", ".join(missing))
        metadata["status"] = "capturing"
        adb_run(args.adb, args.serial,
                ["shell", "perfetto", "--txt", "-c", "-", "-o", remote],
                input_text=config, timeout=duration / 1000 + 60)
        output = args.output_dir / "trace.pftrace"
        adb_run(args.adb, args.serial, ["pull", remote, str(output)], timeout=120)
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("No nonempty trace was retrieved")
        metadata.update(status="captured", trace_bytes=output.stat().st_size)
        result = 0
    except KeyboardInterrupt:
        metadata.update(status="interrupted", error="Interrupted by user")
        result = 130
    except (RuntimeError, OSError) as exc:
        metadata.update(status="failed", error=str(exc).replace(args.serial, "<selected-device>"))
        result = 1
    finally:
        metadata["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (args.output_dir / "metadata.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    raise SystemExit(main())
