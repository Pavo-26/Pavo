"""Independently capture traces, background workload logs or ODPM records."""
from __future__ import annotations

import argparse
import importlib.util
import json
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
NATIVE_PROGRAMS = {"stress-ng", "ffmpeg", "fio", "benchmark_model"}


def trace_module():
    spec = importlib.util.spec_from_file_location("pavo_raw_perfetto", HERE / "collectors/perfetto/collect_perfetto.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def workload_argv(path: Path | None, seconds: int) -> list[str]:
    if path is None:
        return ["/data/local/tmp/stress-ng", "--cpu", "16", "--cpu-method",
                "matrixprod", "--oom-avoid", "--metrics-brief", "--times",
                "--timeout", f"{seconds}s"]
    record = json.loads(path.read_text(encoding="utf-8-sig"))
    argv = record.get("argv")
    if not isinstance(argv, list) or not argv or any(not isinstance(v, str) or not v or "\0" in v for v in argv):
        raise ValueError("Workload configuration requires an argv list of nonempty strings")
    if not argv[0].startswith("/") or Path(argv[0]).name not in NATIVE_PROGRAMS:
        raise ValueError("Use an absolute device path to stress-ng, ffmpeg, fio or benchmark_model")
    return argv


def record_background(adb: str, serial: str, argv: list[str], seconds: int, output: Path):
    # Save the workload stdout and stderr.
    command = "exec timeout -s INT -k 5 " + str(seconds) + " " + shlex.join(argv)
    with (output / "background.stdout.log").open("xb") as stdout, \
         (output / "background.stderr.log").open("xb") as stderr:
        result = subprocess.run([adb, "-s", serial, "shell", command],
                                stdout=stdout, stderr=stderr, timeout=seconds + 30)
    boundary_reached = result.returncode == 124
    return {"remote_command_returncode": result.returncode,
            "raw_stdout": "background.stdout.log", "raw_stderr": "background.stderr.log",
            "capture_complete": result.returncode in (0, 124),
            "workload_success_evaluated": False,
            "status": ("recorded-at-timeout-boundary" if boundary_reached else
                       "recorded" if result.returncode == 0 else "command-ended-nonzero")}


def record_odpm(serial: str, adb: str, seconds: int, interval: float, output: Path):
    from collectors.pixel6.power_breakdown_collector import PowerBreakdownCollector
    collector = PowerBreakdownCollector(output, device_id=serial)
    collector.adb_prefix = [adb, "-s", serial]
    errors = []
    try:
        collector.capture_start()
        collector.start_odpm_sampler(interval)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if collector._sampler_error:
                raise RuntimeError("Raw sampler failed")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except BaseException as exc:
        collector.recording_error = str(exc) or type(exc).__name__
        errors.append(exc)
    finally:
        # Attempt each cleanup independently, even after an interrupted sample.
        for action in (collector.stop_odpm_sampler, collector.capture_end, collector.finalize):
            try:
                action()
            except BaseException as exc:
                errors.append(exc)
    if errors:
        raise RuntimeError("ODPM recording/cleanup failed; raw files retained: " +
                           "; ".join(str(exc) for exc in errors)) from errors[0]
    return {"status": "recorded", "capture_complete": True,
            "raw_inventory": "raw_capture_inventory.json"}


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("trace", "background", "odpm"), required=True)
    parser.add_argument("--serial")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--duration", type=int, help="Seconds; trace uses its config unless overridden, others default to 60")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--trace-config", type=Path)
    parser.add_argument("--workload-config", type=Path)
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    requested_duration = args.duration
    args.duration = args.duration if args.duration is not None else 60
    if not 1 <= args.duration <= 3600 or not 0.1 <= args.interval <= 60:
        parser.error("duration must be 1..3600s and interval 0.1..60s")
    try:
        trace = None
        if args.source == "trace":
            trace = trace_module()
            _, duration_ms, _ = trace.configured_capture(
                args.trace_config or trace.DEFAULT_CONFIG, requested_duration)
            args.duration = duration_ms / 1000
        native_argv = workload_argv(args.workload_config, args.duration) if args.source == "background" else None
        capture_settings = {"kind": "raw-recording", "source": args.source,
                "duration_s": args.duration,
                "interval_s": args.interval, "argv": native_argv}
        if args.source == "odpm":
            capture_settings["sampling_location"] = "device"
            capture_settings["sampler"] = "odpm_sampler.sh"
        if not args.serial or not args.serial.strip() or not args.output_dir:
            raise ValueError("--execute requires --serial and a fresh --output-dir")
        if args.output_dir.resolve().is_relative_to(HERE.parent):
            raise ValueError("Choose an output directory outside the source tree")
        if args.source == "trace":
            # Avoid collisions with the standard-library trace module.
            command = ["--serial", args.serial, "--adb", args.adb,
                       "--output-dir", str(args.output_dir), "--execute"]
            if args.trace_config:
                command += ["--config", str(args.trace_config)]
            if requested_duration is not None:
                command += ["--duration", str(requested_duration)]
            return trace.main(command)
        args.output_dir.mkdir(parents=True, exist_ok=False)
        record = dict(capture_settings, status="failed", capture_complete=False,
                      started_utc=datetime.now(timezone.utc).isoformat())
        try:
            check = subprocess.run([args.adb, "-s", args.serial, "shell", "id -u"],
                                   capture_output=True, text=True, timeout=15, check=True)
            if check.stdout.strip() != "0":
                raise RuntimeError("Root adbd required")
            if args.source == "odpm":
                query = "for f in /sys/bus/iio/devices/iio:device*/energy_value; do [ -r \"$f\" ] && exit 0; done; exit 1"
            else:
                query = "command -v timeout >/dev/null && test -x " + shlex.quote(native_argv[0])
            subprocess.run([args.adb, "-s", args.serial, "shell", query],
                           capture_output=True, timeout=15, check=True)
            if args.source == "background":
                record.update(record_background(args.adb, args.serial, native_argv, args.duration, args.output_dir))
            else:
                record.update(record_odpm(args.serial, args.adb, args.duration, args.interval, args.output_dir))
        except BaseException as exc:
            record["error"] = str(exc)
            raise
        finally:
            record["finished_utc"] = datetime.now(timezone.utc).isoformat()
            (args.output_dir / "recording.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        return 0 if record.get("capture_complete") else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
