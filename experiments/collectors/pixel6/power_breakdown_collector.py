"""Save Pixel 6 raw ODPM counters and device state."""
from __future__ import annotations

import json
import subprocess
import shlex
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PowerBreakdownCollector:
    """Capture Pixel 6 ODPM counters and device state."""

    def __init__(self, output_dir: str | Path, model_path=None, device_id=None):
        if not device_id:
            raise ValueError("An explicit device_id is required for raw recording")
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.adb_prefix = ["adb", "-s", device_id]
        self.snapshot_script = Path(__file__).with_name("power_breakdown_snapshot.sh")
        self.start_snapshot = self.end_snapshot = None
        self.odpm_sampler_started = False
        self.odpm_sampler_local_path = self.output_dir / "odpm_continuous_samples.txt"
        self.records: list[dict[str, Any]] = []
        self._sampler = None
        self.sampler_script = Path(__file__).with_name("odpm_sampler.sh")
        self.remote_samples = None
        self._sampler_error = None
        self.recording_error = None
        self.sampling_interval_s = None
        # model_path is a compatibility argument.

    def _snapshot_command(self, label: str):
        return subprocess.run(
            self.adb_prefix + ["shell", "sh", "-s", label],
            input=self.snapshot_script.read_bytes(), capture_output=True, timeout=30,
        )

    def capture(self, label: str) -> dict[str, Any]:
        if label not in ("start", "end"):
            raise ValueError("Snapshot label must be start or end")
        path = self.output_dir / f"snapshot_bundle_{label}.txt"
        if path.exists():
            raise FileExistsError(path)
        started = datetime.now(timezone.utc).isoformat()
        result = self._snapshot_command(label)
        # Save the device output.
        path.write_bytes(result.stdout)
        (self.output_dir / f"snapshot_{label}.stderr.txt").write_bytes(result.stderr)
        record = {
            "label": label, "path": str(path.resolve()), "bytes": path.stat().st_size,
            "host_started_utc": started,
            "host_finished_utc": datetime.now(timezone.utc).isoformat(),
            "returncode": result.returncode,
        }
        self.records.append(record)
        if result.returncode != 0 or not result.stdout:
            raise RuntimeError("Raw snapshot command failed; output retained")
        return record

    def capture_start(self):
        self.start_snapshot = self.capture("start")
        return self.start_snapshot

    def capture_end(self):
        self.end_snapshot = self.capture("end")
        return self.end_snapshot

    def start_odpm_sampler(self, interval_s=1.0):
        if self.odpm_sampler_started:
            raise RuntimeError("Raw sampler is already running")
        if not math.isfinite(interval_s) or not 0.1 <= interval_s <= 60:
            raise ValueError("Sampling interval must be between 0.1 and 60 seconds")
        if self.odpm_sampler_local_path.exists():
            raise FileExistsError(self.odpm_sampler_local_path)

        # Run the periodic sampler on the phone.
        from device_control import Adb, RemoteProcess
        adb = Adb(self.adb_prefix[0], self.adb_prefix[2])
        # Background shells may inherit ignored SIGINT; the sampler exits cleanly on TERM.
        self._sampler = RemoteProcess(adb, "", self.output_dir, "odpm_sampler",
                                      stop_signal="TERM")
        remote_script = self._sampler.remote + ".sh"
        self.remote_samples = self._sampler.remote + ".samples"
        adb.run(["push", str(self.sampler_script), remote_script])
        self._sampler.command = shlex.join(
            ["sh", remote_script, self.remote_samples, f"{interval_s:g}"])
        self.sampling_interval_s = interval_s
        self._sampler_error = None
        self.odpm_sampler_started = True
        try:
            self._sampler.start()
            time.sleep(min(2.0, interval_s + 0.5))
            self._sampler.check()
            adb.shell("test -s " + shlex.quote(self.remote_samples))
        except BaseException as exc:
            self._sampler_error = exc
            raise

    def stop_odpm_sampler(self):
        if not self.odpm_sampler_started:
            return None
        errors = []
        # Attempt stop and retrieval of existing raw data even after abnormal sampler exit.
        for action in (self._sampler.check, self._sampler.stop):
            try:
                action()
            except BaseException as exc:
                errors.append(exc)
        try:
            if self.odpm_sampler_local_path.exists():
                raise FileExistsError(self.odpm_sampler_local_path)
            self._sampler.adb.run(
                ["pull", self.remote_samples, str(self.odpm_sampler_local_path)])
            if not self.odpm_sampler_local_path.is_file() or not self.odpm_sampler_local_path.stat().st_size:
                raise RuntimeError("ODPM sampler did not produce a nonempty raw file")
        except BaseException as exc:
            errors.append(exc)
        self.odpm_sampler_started = False
        if errors:
            self._sampler_error = RuntimeError("; ".join(str(exc) for exc in errors))
            raise RuntimeError("ODPM sampler failed; local and remote files retained") from self._sampler_error
        return self.odpm_sampler_local_path

    def finalize(self):
        stop_error = None
        if self.odpm_sampler_started:
            try:
                self.stop_odpm_sampler()
            except RuntimeError as exc:
                stop_error = str(exc)
        result = {
            "schema_version": 1, "kind": "raw-odpm-context-inventory",
            "capture_complete": (self.start_snapshot is not None and self.end_snapshot is not None
                                 and self._sampler_error is None and stop_error is None
                                 and self.recording_error is None),
            "recording_error": self.recording_error,
            "sampler_error": str(self._sampler_error) if self._sampler_error else stop_error,
            "snapshots": self.records,
            "requested_sampling_interval_s": self.sampling_interval_s,
            "sampling_location": "device",
            "timing_scope": "device timestamps with a sleep interval between reads",
            "remote_samples": self.remote_samples,
            "continuous_samples": (str(self.odpm_sampler_local_path.resolve())
                                   if self.odpm_sampler_local_path.is_file() else None),
        }
        (self.output_dir / "raw_capture_inventory.json").write_text(
            json.dumps(result, indent=2), encoding="utf-8")
        if stop_error:
            raise RuntimeError(stop_error)
        return result
