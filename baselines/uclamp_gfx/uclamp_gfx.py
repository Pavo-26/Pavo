"""Apply UCLAMP to foreground graphics threads and restore original values afterward."""
from __future__ import annotations

import json
import logging
import re
import shlex
import time
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
PACKAGE_NAME = "com.ss.android.ugc.aweme"
UCLAMP_MAX = 1024
UCLAMP_DAEMON = "/data/local/tmp/uclamp_floor_daemon"
UCLAMP_POLL_INTERVAL_US = 2000
UCLAMP_MAX_GAP_US = 20000
Adb = Any


class ValidationError(RuntimeError):
    """UCLAMP setup or restoration failed."""


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_sched_clamp(text: str) -> dict[str, int | None]:
    def field(name: str) -> int | None:
        match = re.search(rf"^{re.escape(name)}\s*:\s*(\d+)", text, re.MULTILINE)
        return int(match.group(1)) if match else None

    return {
        "requested_min": field("uclamp.min"),
        "requested_max": field("uclamp.max"),
        "effective_min": field("effective uclamp.min"),
        "effective_max": field("effective uclamp.max"),
    }


class UclampGfxSession:
    """Maintain the UCLAMP floor on selected graphics threads."""

    def __init__(
        self,
        adb: Adb,
        output_dir: Path,
        floor: int,
        package_name: str = PACKAGE_NAME,
        app_label: str = "Douyin",
    ) -> None:
        self.adb = adb
        self.output_dir = output_dir
        self.floor = floor
        self.package_name = package_name
        self.app_label = app_label
        self.pid: int | None = None
        self.before: dict[int, dict[str, Any]] = {}
        self.applied: dict[int, dict[str, Any]] = {}
        self.active_tids: tuple[int, ...] = ()
        self.daemon_pid: int | None = None
        self.remote_log: str | None = None
        self.local_log = output_dir / "uclamp_floor_daemon.log"
        self.stop_result: dict[str, Any] | None = None
        self.daemon_segments: list[dict[str, Any]] = []

    def _main_pid(self) -> int:
        pids = self.adb.get_text(f"pidof {self.package_name}").split()
        if len(pids) != 1 or not pids[0].isdigit():
            raise ValidationError(
                f"Expected one {self.app_label} main PID, got {pids or 'none'}"
            )
        return int(pids[0])

    def _read(self, tid: int) -> dict[str, int | None]:
        result = self.adb.shell(f"cat /proc/{tid}/sched", check=False)
        return parse_sched_clamp(result.stdout)

    def _threads(self, pid: int) -> dict[int, str]:
        main_name = self.adb.get_text(f"cat /proc/{pid}/comm") or "main"
        output = self.adb.shell(
            f"grep -H -E '^(RenderThread|hwuiTask)' "
            f"/proc/{pid}/task/*/comm 2>/dev/null || true",
            timeout=60,
        ).stdout
        threads: dict[int, str] = {pid: main_name}
        for line in output.splitlines():
            match = re.match(r"/proc/\d+/task/(\d+)/comm:(.*)", line)
            if match:
                threads[int(match.group(1))] = match.group(2).strip()
        return threads

    def _daemon_alive(self) -> bool:
        return self.daemon_pid is not None and self.adb.shell(
            f"kill -0 {self.daemon_pid}", check=False, timeout=10
        ).returncode == 0

    def _start_daemon(self, tids: tuple[int, ...]) -> None:
        if not tids:
            raise ValidationError("No GFX threads were resolved for UCLAMP")
        token = f"{self.floor}_{self.pid}_{int(time.time() * 1000)}"
        self.remote_log = f"/data/local/tmp/uclamp_floor_{token}.log"
        self.local_log = self.output_dir / f"uclamp_floor_daemon_{len(self.daemon_segments) + 1:03d}.log"
        tid_args = " ".join(str(tid) for tid in tids)
        command = (
            f"setsid {UCLAMP_DAEMON} {self.floor} {UCLAMP_POLL_INTERVAL_US} "
            f"{tid_args} > {shlex.quote(self.remote_log)} 2>&1 < /dev/null "
            "& echo $!"
        )
        result = self.adb.shell(command, timeout=15)
        pids = re.findall(r"(?m)^(\d+)\s*$", result.stdout)
        if not pids:
            raise ValidationError(
                f"Cannot resolve UCLAMP daemon PID: {result.stdout!r} {result.stderr!r}"
            )
        self.daemon_pid = int(pids[-1])
        self.active_tids = tids
        time.sleep(0.1)
        if not self._daemon_alive():
            raise ValidationError("UCLAMP floor daemon exited during startup")

    def _parse_daemon_stats(self) -> dict[str, int]:
        if not self.local_log.exists():
            return {}
        text = self.local_log.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"(?m)^summary\s+(.+)$", text)
        if not match:
            return {}
        return {
            key: int(value)
            for key, value in re.findall(r"(\w+)=(\d+)", match.group(1))
        }

    def _stop_daemon(self) -> dict[str, Any]:
        pid = self.daemon_pid
        forced = False
        if pid is not None and self._daemon_alive():
            self.adb.shell(f"kill -INT {pid}", check=False)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self._daemon_alive():
                time.sleep(0.1)
            if self._daemon_alive():
                forced = True
                self.adb.shell(f"kill -KILL {pid}", check=False)
                time.sleep(0.1)

        alive_after_stop = self._daemon_alive()
        copied = False
        remote_log = self.remote_log
        copy_error = None
        if remote_log:
            try:
                self.adb.shell(f"chmod a+r {shlex.quote(remote_log)}", check=False)
                result = self.adb.run(
                    ["pull", remote_log, str(self.local_log)], check=False, timeout=30)
                copied = result.returncode == 0 and self.local_log.is_file()
                if copied:
                    self.adb.shell(f"rm -f {shlex.quote(remote_log)}", check=False)
            except Exception as exc:
                # Losing the log must not prevent restoration of task clamps.
                copy_error = str(exc)
        stats = self._parse_daemon_stats() if copied else {}
        self.daemon_pid = pid if alive_after_stop else None
        self.remote_log = None
        record = {
            "pid": pid,
            "forced_kill": forced,
            "alive_after_stop": alive_after_stop,
            "stats": stats,
            "log": str(self.local_log.resolve()),
            "log_copied": copied,
            "log_copy_error": copy_error,
            "remote_log_retained": remote_log if not copied else None,
        }
        if pid is not None or remote_log is not None:
            self.daemon_segments.append(record)
        return record

    @staticmethod
    def _segment_healthy(segment: dict) -> bool:
        stats = segment.get("stats") or {}
        return (segment.get("log_copied", False)
                and not segment.get("forced_kill") and not segment.get("alive_after_stop")
                and stats.get("loops", 0) > 0
                and all(stats.get(key, 0) == 0 for key in (
                    "get_errors", "set_errors", "verify_errors", "affinity_errors",
                    "priority_errors", "realtime_errors"))
                and stats.get("max_gap_us", UCLAMP_MAX_GAP_US + 1) <= UCLAMP_MAX_GAP_US)

    def refresh(self) -> None:
        self.pid = self._main_pid()
        selected = self._threads(self.pid)
        tids = tuple(sorted(selected))
        for tid, name in selected.items():
            if tid not in self.before:
                values = self._read(tid)
                if values["requested_min"] is None or values["requested_max"] is None:
                    raise ValidationError("Cannot save original UCLAMP settings")
                self.before[tid] = {
                    "thread_name": name,
                    **values,
                }
        write_json(self.output_dir / "uclamp_gfx_before.json", self.before)

        if self.daemon_pid is not None and (
            tids != self.active_tids or not self._daemon_alive()
        ):
            segment = self._stop_daemon()
            if not self._segment_healthy(segment):
                raise ValidationError("A UCLAMP maintenance segment failed; see its log")
        if self.daemon_pid is None:
            self._start_daemon(tids)

        verification_attempts = 0
        for verification_attempts in range(1, 6):
            self.applied = {}
            for tid, name in selected.items():
                values = self._read(tid)
                valid = (
                    isinstance(values["effective_min"], int)
                    and values["effective_min"] >= self.floor
                    and values["effective_max"] == UCLAMP_MAX
                )
                self.applied[tid] = {
                    "thread_name": name,
                    **values,
                    "valid": valid,
                }
            if self.applied and all(
                item["valid"] for item in self.applied.values()
            ):
                break
            time.sleep(0.01)
        if not self.applied or not all(item["valid"] for item in self.applied.values()):
            raise ValidationError(f"UCLAMP-GFX verification failed: {self.applied}")

        write_json(
            self.output_dir / "uclamp_gfx_applied.json",
            {
                "pid": self.pid,
                "floor": self.floor,
                "uclamp_max": UCLAMP_MAX,
                "target_roles": "main/UI + RenderThread + hwuiTask*",
                "control_scope": (
                    "sched_setattr floor with Android boosts above the floor preserved"
                ),
                "daemon_pid": self.daemon_pid,
                "poll_interval_us": UCLAMP_POLL_INTERVAL_US,
                "verification_attempts": verification_attempts,
                "before": self.before,
                "applied": self.applied,
            },
        )
        LOGGER.info(
            "UCLAMP %d verified on %d GFX threads with daemon PID %d",
            self.floor,
            len(self.applied),
            self.daemon_pid,
        )

    def stop(self, *, best_effort: bool = False) -> dict[str, Any]:
        if self.stop_result is not None:
            if not self.stop_result["restored"] and not best_effort:
                raise ValidationError("Earlier UCLAMP cleanup failed")
            return self.stop_result

        before_restore: dict[int, dict[str, Any]] = {}
        active_ok = False
        verification_attempts = 0
        for verification_attempts in range(1, 6):
            before_restore = {}
            for tid, before in self.before.items():
                exists = self.adb.shell(
                    f"test -d /proc/{tid}", check=False, timeout=10
                ).returncode == 0
                if not exists:
                    before_restore[tid] = {
                        "thread_name": before["thread_name"],
                        "exited": True,
                    }
                    continue
                values = self._read(tid)
                before_restore[tid] = {
                    "thread_name": before["thread_name"],
                    **values,
                    "valid": (
                        isinstance(values["effective_min"], int)
                        and values["effective_min"] >= self.floor
                        and values["effective_max"] == UCLAMP_MAX
                    ),
                }
            active_ok = all(
                item.get("exited") or item.get("valid")
                for item in before_restore.values()
            )
            if active_ok:
                break
            # Android may rewrite requested uclamp between the daemon poll and
            # this snapshot; retry while the maintainer is still active.
            time.sleep(0.01)

        daemon = self._stop_daemon()
        daemon_ok = bool(self.daemon_segments) and all(
            self._segment_healthy(segment) for segment in self.daemon_segments)


        restored: dict[int, dict[str, Any]] = {}
        for tid, before in self.before.items():
            exists = self.adb.shell(
                f"test -d /proc/{tid}", check=False, timeout=10
            ).returncode == 0
            if not exists:
                restored[tid] = {
                    "thread_name": before["thread_name"],
                    "exited": True,
                }
                continue
            requested_min = int(before["requested_min"])
            requested_max = int(before["requested_max"])
            attempts: list[dict[str, Any]] = []
            restore_command_ok = False
            for attempt in range(1, 6):
                result = self.adb.shell(
                    f"/system/bin/uclampset -p {tid} -m {requested_min} "
                    f"-M {requested_max}",
                    check=False,
                )
                attempts.append(
                    {
                        "attempt": attempt,
                        "returncode": result.returncode,
                        "stdout": result.stdout.strip(),
                        "stderr": result.stderr.strip(),
                    }
                )
                values = self._read(tid)
                matched = (values["requested_min"] == requested_min
                           and values["requested_max"] == requested_max)
                attempts[-1]["readback_matches"] = matched
                if result.returncode == 0 and matched:
                    restore_command_ok = True
                    break
                if self.adb.shell(
                    f"test -d /proc/{tid}", check=False, timeout=10
                ).returncode != 0:
                    break
                time.sleep(0.01)
            restored[tid] = {
                "thread_name": before["thread_name"],
                **self._read(tid),
                "restore_command_ok": restore_command_ok,
                "restore_attempts": attempts,
            }
        restore_ok = all(
            item.get("exited") or (item.get("restore_command_ok")
                and item.get("requested_min") == self.before[tid]["requested_min"]
                and item.get("requested_max") == self.before[tid]["requested_max"])
            for tid, item in restored.items()
        )
        ok = active_ok and daemon_ok and restore_ok
        self.stop_result = {
            "restored": ok,
            "restore_readback_verified": restore_ok,
            "effective_floor_valid_before_restore": active_ok,
            "floor_verification_attempts": verification_attempts,
            "daemon_healthy": daemon_ok,
            "daemon": daemon,
            "daemon_segments": self.daemon_segments,
            "before_restore": before_restore,
            "targets": restored,
        }
        write_json(self.output_dir / "uclamp_gfx_restored.json", self.stop_result)
        if not ok and not best_effort:
            raise ValidationError(
                "UCLAMP-GFX daemon or restoration failed: "
                f"active_ok={active_ok}, daemon={daemon}, restored={restored}"
            )
        return self.stop_result
