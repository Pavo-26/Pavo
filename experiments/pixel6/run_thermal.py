"""Pixel 6 temperature experiment: Douyin, Default/Pavo, five runs each."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main_protocol import BACKGROUND_TIMEOUT_S, INTER_RUN_DELAY_S, WORKERS, select, validate_runtime
from run_main import make_runs, run_window, summarize, write_json

THERMAL_POLICIES = ("android_default", "pavo")
BASELINE_DURATION_S = 60
SAMPLE_INTERVAL_MS = 1000
SAMPLER_TIMEOUT_S = BASELINE_DURATION_S + BACKGROUND_TIMEOUT_S + 60
SENSORS = (
    ("tz0_mC", "thermal_zone0", "BIG"),
    ("tz1_mC", "thermal_zone1", "MID"),
    ("tz2_mC", "thermal_zone2", "LITTLE"),
    ("skin_mC", "thermal_zone16", "quiet_therm"),
    ("batt_mC", "thermal_zone25", "battery"),
)


class ThermalCapture:
    """Capture idle and workload phases around the common App/PerfDog flow."""

    def __init__(self, driver, sampler, binary):
        self.driver, self.sampler, self.binary = driver, sampler, binary
        self.run = driver.run
        self.remote_output = sampler.remote + ".thermal"
        self.started = False
        self.complete = False
        self.workload_ended = False
        self.export_complete = False
        self.stop_error = None
        self.phases = []
        self.files = {}

    def __getattr__(self, name):
        return getattr(self.driver, name)

    def mark(self, label):
        before = time.monotonic()
        host_epoch = time.time()
        command = "epoch=$(date +%s); "
        if label in ("BASELINE_START", "BASELINE_END", "WORKLOAD_START", "WORKLOAD_END"):
            command += (
                "printf '%s=%s\\n' " + shlex.quote(label) + ' "$epoch" >> ' +
                shlex.quote(self.remote_output + "/markers.txt") + "; ")
        values = self.driver.adb.get_text(command + 'printf "%s\\n" "$epoch"; cat /proc/uptime').splitlines()
        self.phases.append({
            "phase": label,
            "host_epoch_s": host_epoch,
            "device_epoch_s": int(values[0]),
            "host_monotonic_before_s": before,
            "host_monotonic_after_s": time.monotonic(),
            "device_uptime_raw": values[1],
        })

    def prepare(self):
        adb = self.driver.adb
        adb.verify_binary({"path": self.binary})
        if adb.get_text(shlex.join([self.binary, "--describe"])) != "sysmon_native pixel6-thermal-v1":
            raise RuntimeError("Build the supplied native recorder in an independent directory")
        sources = []
        for field, zone, expected_type in SENSORS:
            root = "/sys/class/thermal/" + zone
            actual_type = adb.read(root + "/type")
            raw_value = adb.read(root + "/temp")
            if actual_type != expected_type:
                raise RuntimeError("Thermal sensor mapping differs: " + zone)
            if int(raw_value) == -1:
                raise RuntimeError("Thermal sensor is unreadable: " + zone)
            sources.append({
                "field": field, "path": root + "/temp", "type": actual_type,
                "preparation_value_raw": raw_value,
            })
        write_json(self.driver.directory / "thermal_sources.json", {
            "sensors": sources,
            "frequency_policies": {"policy0": "LITTLE", "policy4": "MID", "policy6": "BIG"},
            "skin_source": "quiet_therm",
        })
        self.driver.prepare_device()
        self.sampler.command = shlex.join([
            self.binary, self.remote_output, str(SAMPLER_TIMEOUT_S),
            str(SAMPLE_INTERVAL_MS), "0", "1",
        ])
        self.started = True
        self.sampler.start()
        time.sleep(2)
        self.sampler.check()
        self.mark("BASELINE_START")
        time.sleep(BASELINE_DURATION_S)
        self.sampler.check()
        self.mark("BASELINE_END")
        self.driver.prepare_app()

    def start_background(self):
        self.sampler.check()
        self.driver.start_background()
        self.mark("WORKLOAD_START")

    def start_interaction(self):
        self.mark("before_interaction")
        return self.driver.start_interaction()

    def check_health(self):
        self.driver.check_health()
        if self.started:
            self.sampler.check()

    def stop_recording(self):
        self.driver.stop_recording()
        self.mark("after_perfdog_stop")

    def stop_background(self):
        if self.driver.background is not None:
            self.driver.background.stop()
            self.driver.background = None

    def stop_thermal(self):
        if not self.started:
            return
        errors = []
        for action in (self.sampler.check, self.sampler.stop):
            try:
                action()
            except BaseException as exc:
                errors.append(str(exc))
        for name in ("sysmon.csv", "sync_start.txt", "markers.txt"):
            target = self.driver.directory / name
            try:
                self.driver.adb.run(["pull", self.remote_output + "/" + name, str(target)])
                if not target.is_file() or not target.stat().st_size:
                    raise RuntimeError("Missing raw thermal file: " + name)
                self.files[name] = str(target.resolve())
            except BaseException as exc:
                errors.append(str(exc))
        if "sync_start.txt" in self.files:
            # Check the recorder completion record.
            sync = (self.driver.directory / "sync_start.txt").read_text(encoding="utf-8")
            if ("boottime_ns1=" not in sync or "clock_error=0" not in sync
                    or "write_error=0" not in sync):
                errors.append("Native recorder did not publish a clean completion record")
        self.started = False
        self.complete = not errors
        if errors:
            self.stop_error = "; ".join(errors)
            raise RuntimeError("Thermal capture failed; available raw files retained: " + self.stop_error)

    def export(self):
        result = self.driver.export()
        time.sleep(5)
        self.sampler.check()
        if self.driver.background is not None:
            self.driver.background.check()
        self.driver.flow.close()
        self.mark("WORKLOAD_END")
        self.workload_ended = True
        self.stop_background()
        self.stop_thermal()
        if not self.complete:
            raise RuntimeError("Raw thermal capture did not finish")
        result.update(
            thermal_capture_complete=True,
            thermal_raw_files=self.files, thermal_phases=self.phases,
            idle_baseline_duration_s=BASELINE_DURATION_S,
        )
        self.export_complete = True
        return result

    def cleanup(self):
        errors = []
        # Restore workload and device controls even if the sampler fails.
        for action in (self.stop_background, self.stop_thermal, self.driver.cleanup):
            try:
                action()
            except BaseException as exc:
                errors.append(str(exc))
        write_json(self.driver.directory / "thermal_capture.json", {
            "capture_complete": self.complete and self.workload_ended and self.export_complete and not errors,
            "idle_baseline_duration_s": BASELINE_DURATION_S,
            "sampling_interval_ms": SAMPLE_INTERVAL_MS,
            "remote_output": self.remote_output,
            "raw_files": self.files, "phases": self.phases,
            "sampler_error": self.stop_error, "cleanup_errors": errors,
        })
        if errors:
            raise RuntimeError("Thermal cleanup failed: " + "; ".join(errors))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policies", default="all")
    parser.add_argument("--serial")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--sysmon-binary", default="/data/local/tmp/pavo-thermal/sysmon_native")
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        policies = select(args.policies, THERMAL_POLICIES)
        runs = make_runs("pixel6", ["dy"], policies)
        if not args.serial or not args.runtime_config or not args.output_root:
            raise ValueError("--execute requires --serial, --runtime-config and a new --output-root")
        if not args.sysmon_binary.startswith("/") or args.sysmon_binary.rsplit("/", 1)[-1] != "sysmon_native":
            raise ValueError("--sysmon-binary must be an absolute Android path to sysmon_native")
        config = json.loads(args.runtime_config.read_text(encoding="utf-8-sig"))
        validate_runtime(config, "pixel6", policies, ["dy"])
        package = Path(__file__).resolve().parents[2]
        if args.output_root.resolve().is_relative_to(package):
            raise ValueError("Choose an output directory outside the source tree")
        from device_control import DeviceDriver, RemoteProcess
        args.output_root.mkdir(parents=True, exist_ok=False)
        rows = []
        for index, run in enumerate(runs, 1):
            directory = args.output_root / f"{index:03d}_dy_{run['policy']}_r{run['repetition']}"
            directory.mkdir()
            row = dict(run, status="failed")
            try:
                driver = DeviceDriver(
                    platform="pixel6", run=run, config=config, directory=directory,
                    serial=args.serial, adb=args.adb, workers=WORKERS,
                )
                sampler = RemoteProcess(driver.adb, "", directory, "sysmon_native", stop_signal="TERM")
                capture = ThermalCapture(driver, sampler, args.sysmon_binary)
                row.update(run_window(capture), status="valid")
            except BaseException as exc:
                row["error"] = str(exc)
                raise
            finally:
                write_json(directory / "capture.json", row)
                rows.append(row)
                write_json(args.output_root / "run_inventory.json", rows)
            if index < len(runs) and runs[index]["policy"] == run["policy"]:
                time.sleep(INTER_RUN_DELAY_S)
        write_json(args.output_root / "capture_inventory.json", summarize(rows, runs))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
