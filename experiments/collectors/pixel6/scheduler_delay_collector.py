#!/usr/bin/env python3
"""Capture sched_scx_delay_stats and save raw trace and ftrace state."""

from __future__ import annotations

import json
import shlex
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


TRACE_ROOTS = ("/sys/kernel/tracing", "/sys/kernel/debug/tracing")


class SchedulerDelayCollector:
    """Capture ftrace and export the raw trace and per-CPU state."""

    def __init__(
        self,
        adb: Any,
        output_dir: Path,
        run_id: str,
        package_name: str,
        chroot_dir: str,
        *,
        buffer_size_kb: int = 32768,
        keep_remote: bool = False,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
            raise ValueError("Use a simple unique run ID")
        if type(buffer_size_kb) is not int or not 1 <= buffer_size_kb <= 65536:
            raise ValueError("Trace buffer must be 1..65536 KiB per CPU")
        self.instance_name = "pavo_" + uuid.uuid4().hex
        self.trace_root = TRACE_ROOTS[0] + "/instances/" + self.instance_name
        self.delay_event = self.trace_root + "/events/sched/sched_scx_delay_stats"
        self.instance_created = False
        self.remote_dumped = False
        self.adb = adb
        self.output_dir = output_dir
        self.run_id = run_id
        self.package_name = package_name
        self.chroot_dir = chroot_dir
        self.buffer_size_kb = buffer_size_kb
        self.keep_remote = keep_remote
        self.remote_trace = f"/data/local/tmp/{run_id}.delay.trace.gz"
        self.remote_stats = f"/data/local/tmp/{run_id}.delay.stats"
        self.original_buffer_size_kb: int | None = None
        self.prepared = False
        self.started = False
        self.stopped = False
        self.started_at: str | None = None
        self.stopped_at: str | None = None
        self.app_threads: dict[int, dict[str, Any]] = {}

    def _run_adeb(
        self,
        command: str,
        *,
        check: bool = True,
        timeout: float = 30,
    ) -> Any:
        return self.adb.shell(
            f"chroot {shlex.quote(self.chroot_dir)} /bin/bash --noprofile --norc -c "
            f"{shlex.quote(command)}",
            check=check,
            timeout=timeout,
        )

    def _restore_tracefs(self) -> None:
        """Remove only the trace instance created for this capture."""
        if self.instance_created:
            self._run_adeb(
                f"set -eu; echo 0 > {self.trace_root}/tracing_on; "
                f"echo 0 > {self.trace_root}/events/enable; "
                f"rmdir {self.trace_root}", timeout=30)
            self.instance_created = False

    def prepare(self) -> dict[str, Any]:
        if self.prepared or self.instance_created:
            raise RuntimeError("Trace collector is already prepared")
        # tracefs must already be visible in chroot; do not mount it or change existing traces.
        root = self._run_adeb(
            "for root in " + shlex.join(TRACE_ROOTS) + "; do "
            'if test -r "$root/events/sched/sched_scx_delay_stats/format" '
            '&& test -d "$root/instances"; then printf "%s\\n" "$root"; break; fi; done'
        ).stdout.strip()
        if root not in TRACE_ROOTS:
            raise RuntimeError("sched_scx_delay_stats is unavailable at the mounted tracefs paths")
        self.trace_root = root + "/instances/" + self.instance_name
        self.delay_event = self.trace_root + "/events/sched/sched_scx_delay_stats"
        self._run_adeb(
            f"set -eu; mkdir {self.trace_root}")
        self.instance_created = True
        try:
            self._run_adeb(
                f"set -eu; test -w {self.delay_event}/enable; "
                f"echo 0 > {self.trace_root}/tracing_on; "
                f"echo {self.buffer_size_kb} > {self.trace_root}/buffer_size_kb; "
                f"echo 1 > {self.delay_event}/enable")
            actual = self._run_adeb(
                f"set -eu; cat {self.trace_root}/buffer_size_kb; "
                f"cat {self.trace_root}/buffer_total_size_kb").stdout.splitlines()
            self.prepared = True
            return {
                "event": "sched/sched_scx_delay_stats",
                "owned_instance": self.trace_root,
                "requested_buffer_size_kb_per_cpu": self.buffer_size_kb,
                "actual_buffer_size_kb_per_cpu": int(actual[0]),
                "actual_buffer_total_size_kb": int(actual[1]),
                "global_trace_state_modified": False, "raw_unit": "ns",
            }
        except BaseException:
            self._restore_tracefs()
            raise

    def _dump_remote(self) -> None:
        if self.remote_dumped:
            return
        command = (
            "set -euo pipefail; "
            f"for f in {self.trace_root}/per_cpu/cpu*/stats; do "
            f"echo '### '$f; cat $f; done > {shlex.quote(self.remote_stats)}; "
            f"cat {self.trace_root}/trace | gzip -1 > {shlex.quote(self.remote_trace)}")
        self._run_adeb(command, timeout=180)
        self.remote_dumped = True

    def _collect_app_threads(self) -> dict[int, dict[str, Any]]:
        command = (
            f"for p in $(/system/bin/pidof {shlex.quote(self.package_name)}); do "
            # Android toybox CMD is the thread name; COMM and NAME are process fields.
            '/system/bin/ps -T -w -p "$p" -o PID,TID,CMD || exit; done'
        )
        result = self.adb.shell(command, timeout=30)
        threads: dict[int, dict[str, Any]] = {}
        for line in result.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                continue
            tgid, tid, comm = int(fields[0]), int(fields[1]), fields[2].strip()
            threads[tid] = {"tgid": tgid, "tid": tid, "comm": comm}
        if not threads:
            raise RuntimeError(f"No threads found for {self.package_name}")
        return threads

    def start(self) -> None:
        if not self.prepared:
            raise RuntimeError("Delay trace was not prepared")
        self.app_threads = self._collect_app_threads()
        self._run_adeb(f"echo 1 > {self.trace_root}/tracing_on")
        self.started = True
        self.started_at = datetime.now().astimezone().isoformat()

    def stop_capture(self) -> None:
        if not self.started or self.stopped:
            return
        self._run_adeb(f"echo 0 > {self.trace_root}/tracing_on")
        self.stopped = True
        self.stopped_at = datetime.now().astimezone().isoformat()

    def finish(self) -> dict[str, Any]:
        if not self.prepared:
            raise RuntimeError("Delay trace was requested but never prepared")
        if not self.started:
            raise RuntimeError("Delay trace was prepared but never started")
        self.stop_capture()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        local_trace = self.output_dir / f"{self.run_id}.delay.trace.gz"
        local_stats = self.output_dir / f"{self.run_id}.delay.stats"
        local_threads = self.output_dir / f"{self.run_id}.app_threads.json"
        local_summary = self.output_dir / f"{self.run_id}.delay_capture.json"
        if any(path.exists() for path in (local_trace, local_stats, local_threads, local_summary)):
            raise FileExistsError("Raw delay output already exists; use a new run ID")

        self._dump_remote()
        try:
            for remote, local in ((self.remote_trace, local_trace), (self.remote_stats, local_stats)):
                result = self.adb.run(["pull", remote, str(local)], timeout=240)
                if result.returncode != 0:
                    raise RuntimeError("Raw trace pull failed; remote files retained")
        finally:
            # Dump succeeded: removing the instance cannot erase the remote copy.
            self._restore_tracefs()

        if not local_trace.is_file() or local_trace.stat().st_size == 0:
            raise RuntimeError(f"Delay trace is missing or empty: {local_trace}")
        if not local_stats.is_file():
            raise RuntimeError(f"Raw ftrace stats file is missing: {local_stats}")

        local_threads.write_text(
            json.dumps(list(self.app_threads.values()), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        result = {
            "event": "sched/sched_scx_delay_stats",
            "capture_started_at": self.started_at,
            "capture_stopped_at": self.stopped_at,
            "trace_path": str(local_trace.resolve()),
            "trace_size_bytes": local_trace.stat().st_size,
            "stats_path": str(local_stats.resolve()),
            "app_threads_path": str(local_threads.resolve()),
            "capture_complete": True,
        }
        local_summary.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        result["inventory_path"] = str(local_summary.resolve())

        if not self.keep_remote:
            self.adb.shell(
                f"rm -f {shlex.quote(self.remote_trace)} {shlex.quote(self.remote_stats)}",
                check=False,
            )
        self.prepared = False
        return result

    def abort(self) -> None:
        self.stop_capture()
        if self.started and self.instance_created:
            # If this fails, keep the owned instance and its buffer for recovery.
            self._dump_remote()
        self._restore_tracefs()
        self.prepared = False
        # Keep remote files after interrupted or failed captures.
