"""Prepare the device, workload and scheduler through ADB, then restore settings."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from main_protocol import (
    APPS, DURATION_S, PERFDOG_VERSION,
    MIN_BATTERY_PCT, BACKGROUND_TIMEOUT_S, CleanupError,
)
from perfdog.checks import validate_ui_config
from app_runner import AppFlow, validate_inputs
from ztt_runner import ZttSession

ARTIFACT_ROOT = Path(__file__).resolve().parents[1]
if str(ARTIFACT_ROOT) not in sys.path:
    sys.path.insert(0, str(ARTIFACT_ROOT))

CPU_ROOT = "/sys/devices/system/cpu/cpufreq/"
SCX_STATE = "/sys/kernel/sched_ext/state"


def surfaceflinger_layers(raw: str, package: str) -> list[str]:
    # Android's RequestedLayerState wrapper is not part of the --latency name.
    candidates = []
    for line in raw.splitlines():
        name = line.strip().split("Layer ", 1)[-1].strip()
        if name.startswith("[") and name.endswith("]"):
            name = name[1:-1]
        if name.startswith("RequestedLayerState{"):
            name = name[len("RequestedLayerState{"):]
            for marker in (" parentId=", " relativeParentId=", " z="):
                name = name.split(marker, 1)[0]
            name = name.rstrip("}").strip()
        if package not in name or name.startswith("["):
            continue
        if any(token in name for token in (
                "ActivityRecordInputSink", "ActivityRecord{", "Background",
                "Letterbox", "SnapshotStartingWindow", "StartingWindow",
                "animation-leash")):
            continue
        if "SplashActivity" in name and "SurfaceView" not in name:
            continue
        candidates.append(name)
    ordered = sorted(set(candidates), key=lambda name: (
        "SurfaceView" not in name, "BLAST" not in name, "/" not in name, len(name), name))
    surfaces = [name for name in ordered if "SurfaceView" in name]
    return surfaces or ordered


class Adb:
    def __init__(self, executable: str, serial: str):
        self.serial = serial
        self.prefix = [executable, "-s", serial]

    def run(self, args: list[str], *, check=True, timeout=10):
        result = subprocess.run(self.prefix + args, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("ADB command failed: " + result.stderr.strip())
        return result

    def shell(self, command: str, *, check=True, timeout=10):
        command = 'export PATH="$PATH:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"; ' + command
        return self.run(["shell", command], check=check, timeout=timeout)

    def get_text(self, command: str, *, timeout=10) -> str:
        return self.shell(command, timeout=timeout).stdout.strip()

    def read(self, path: str) -> str:
        return self.get_text("cat " + shlex.quote(path))

    def write(self, path: str, value: Any) -> None:
        self.shell("printf '%s' " + shlex.quote(str(value)) + " > " + shlex.quote(path))
        if self.read(path) != str(value):
            raise RuntimeError("Device write/readback mismatch: " + path)

    def verify_binary(self, record: dict[str, Any], *, chroot: str | None = None) -> None:
        path = shlex.quote(record["path"])
        prefix = "chroot " + shlex.quote(chroot) + " " if chroot else ""
        if self.shell(prefix + "test -x " + path, check=False).returncode:
            raise RuntimeError("Missing executable: " + record["path"])


class RemoteProcess:
    def __init__(self, adb: Adb, command: str, directory: Path, name: str,
                 *, stop_signal: str = "INT"):
        if stop_signal not in ("INT", "TERM"):
            raise ValueError("Unsupported process stop signal")
        self.adb, self.command, self.directory, self.name = adb, command, directory, name
        self.stop_signal = stop_signal
        self.pid: int | None = None
        self.group_verified = False
        self.remote = "/data/local/tmp/pavo_run_" + uuid.uuid4().hex

    def start(self) -> None:
        inner = "cd /data/local/tmp || exit 1; echo $$ > " + self.remote + ".pid; exec " + self.command
        self.adb.shell("setsid sh -c " + shlex.quote(inner) +
                       " > " + self.remote + ".log 2>&1 < /dev/null &")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.adb.shell("cat " + self.remote + ".pid", check=False)
            if result.stdout.strip().isdigit():
                self.pid = int(result.stdout.strip())
                # setsid must own this exact group; never signal an inherited group.
                stat = self.adb.read(f"/proc/{self.pid}/stat").rsplit(") ", 1)[-1].split()
                if len(stat) < 4 or int(stat[2]) != self.pid or int(stat[3]) != self.pid:
                    raise RuntimeError(self.name + " did not obtain an isolated session")
                self.group_verified = True
                self.check()
                return
            time.sleep(0.1)
        raise RuntimeError(self.name + " did not publish its PID")

    def alive(self) -> bool:
        return self.pid is not None and self.adb.shell(f"kill -0 {self.pid}", check=False).returncode == 0

    def check(self) -> None:
        if not self.alive():
            raise RuntimeError(self.name + " exited early")

    def stop(self) -> None:
        if not self.group_verified:
            if self.pid is None:
                result = self.adb.shell("cat " + self.remote + ".pid", check=False)
                if result.stdout.strip().isdigit():
                    self.pid = int(result.stdout.strip())
                    stat = self.adb.read(f"/proc/{self.pid}/stat").rsplit(") ", 1)[-1].split()
                    self.group_verified = (len(stat) >= 4 and int(stat[2]) == self.pid
                                           and int(stat[3]) == self.pid)
            if not self.group_verified:
                raise RuntimeError(self.name + " cleanup cannot verify its process group; remote logs retained")
        def group_alive():
            return self.adb.shell(f"kill -0 -- -{self.pid}", check=False).returncode == 0
        forced = False
        if group_alive():
            self.adb.shell(f"kill -{self.stop_signal} -- -{self.pid}", check=False)
            deadline = time.monotonic() + 10
            while group_alive() and time.monotonic() < deadline:
                time.sleep(0.1)
            if group_alive():
                forced = True
                self.adb.shell(f"kill -KILL -- -{self.pid}", check=False)
        copied = self.adb.run(["pull", self.remote + ".log", str(self.directory / (self.name + ".log"))], check=False)
        if copied.returncode == 0 and not group_alive():
            self.adb.shell("rm -f " + self.remote + ".pid " + self.remote + ".log", check=False)
        if forced or group_alive() or copied.returncode:
            raise RuntimeError(self.name + " did not stop cleanly")


def verify_perfdog(executable: Path, gui: Any) -> dict[str, Any]:
    if os.name != "nt":
        raise RuntimeError("The PerfDog GUI controller requires Windows")
    if not executable.is_file():
        raise RuntimeError("PerfDog executable is missing")
    window = gui.focus_window()
    if not re.search(r"\bv?11\.2\.250408\b", window["title"]):
        raise RuntimeError("The active PerfDog window is not v" + PERFDOG_VERSION)
    return {"version": PERFDOG_VERSION, "window_title": window["title"]}


class DeviceDriver:
    def __init__(self, *, platform: str, run: dict[str, Any], config: dict[str, Any],
                 directory: Path, serial: str, adb: str, workers: int,
                 app_info: tuple[str, str, str] | None = None):
        self.platform, self.run, self.config = platform, run, config
        self.directory, self.workers = directory, workers
        self.adb = Adb(adb, serial)
        self.adb_executable = adb
        self.app_info = app_info if app_info is not None else APPS[run["app"]]
        self.package = self.app_info[1]
        self.policy = run["policy"]
        self.flow = self.perfdog = self.background = self.scheduler = self.uclamp = self.ztt = None
        self.saved: dict[str, str] = {}
        self.display_saved: dict[str, str] = {}
        self.readiness: list[dict[str, Any]] = []
        self.exported = False
        self.prepared = False
        self.background_prepared = False
        self.interaction_started_monotonic_s = None
        self.interaction_rounds_offset = 0
        self.background_start_requested_monotonic_s = None
        os.environ["ANDROID_SERIAL"] = serial
        if Path(adb).is_absolute():
            os.environ["PATH"] = str(Path(adb).parent) + os.pathsep + os.environ["PATH"]

    def check_environment(self) -> None:
        adb, env = self.adb, self.config["environment"]
        if adb.get_text("id -u") != "0" or adb.read(SCX_STATE) != "disabled":
            raise RuntimeError("Root adbd and no active sched_ext scheduler are required")
        expected_android = "15" if self.platform == "pixel6" else "16"
        for command, expected in (
            ("getprop ro.build.version.release", expected_android),
            ("getprop ro.build.fingerprint", env["fingerprint"]),
            ("uname -r", env["kernel_release"]),
        ):
            if adb.get_text(command) != expected:
                raise RuntimeError("Device environment does not match the runtime configuration")
        if self.platform == "pixel6":
            debian = adb.get_text("chroot " + shlex.quote(env["chroot"]) + " cat /etc/debian_version")
            if debian != env["debian_version"]:
                raise RuntimeError("Installed Debian version differs from the runtime configuration")
        battery = adb.get_text("dumpsys battery")
        if re.search(r"(AC|USB|Wireless) powered:\s*true", battery):
            raise RuntimeError("Unplug external power before device-power measurements")
        if "enabled" not in adb.get_text("cmd wifi status").lower():
            raise RuntimeError("Wi-Fi must be enabled")
        version = adb.get_text("dumpsys package " + shlex.quote(self.package))
        match = re.search(r"versionName=([^\s]+)", version)
        if not match or match.group(1) != self.config["apps"][self.run["app"]]["version_name"]:
            raise RuntimeError("Installed App version differs from the frozen run configuration")
        if adb.get_text("pidof stress-ng || true"):
            raise RuntimeError("An unrelated stress-ng workload is already running")
        adb.verify_binary(self.config["stress_ng"])

    def check_battery(self) -> dict[str, Any]:
        """Check battery capacity and external power."""
        powered = self.adb.get_text("dumpsys battery")
        if re.search(r"(AC|USB|Wireless) powered:\s*true", powered):
            raise RuntimeError("Unplug external power before device-power measurements")
        capacity = int(self.adb.read("/sys/class/power_supply/battery/capacity"))
        state = {"capacity": capacity}
        (self.directory / "pre_run_battery.json").write_text(
            json.dumps([state], indent=2), encoding="utf-8")
        if capacity < MIN_BATTERY_PCT:
            raise RuntimeError(f"Battery {capacity}% is below the {MIN_BATTERY_PCT}% floor")
        return state

    def save_and_set_controls(self) -> None:
        policies = ("policy0", "policy4", "policy6") if self.platform == "pixel6" else ("policy0", "policy6")
        governor = "walt" if self.policy == "stock" else ("performance" if self.policy == "performance" else "schedutil")
        for policy in policies:
            root = CPU_ROOT + policy + "/"
            for key in ("scaling_governor", "scaling_min_freq", "scaling_max_freq"):
                self.saved[root + key] = self.adb.read(root + key)
            if governor not in self.adb.read(root + "scaling_available_governors").split():
                raise RuntimeError("Required governor is unavailable")
        if self.platform == "oneplus15":
            for policy, cpus, capacity in (("policy0", list(range(6)), "762"), ("policy6", [6, 7], "1024")):
                related = [int(c) for c in self.adb.read(CPU_ROOT + policy + "/related_cpus").split()]
                if related != cpus:
                    raise RuntimeError("Unexpected CPUs in the OnePlus cpufreq policy")
                for cpu in cpus:
                    if self.adb.read(f"/sys/devices/system/cpu/cpu{cpu}/cpu_capacity") != capacity:
                        raise RuntimeError("Unexpected OnePlus CPU capacity")
        if self.policy == "ztt":
            paths = ["/sys/class/misc/mali0/device/" + key for key in
                     ("scaling_min_freq", "scaling_max_freq", "dvfs_period")]
            paths += [CPU_ROOT + p + "/" + governor_name + "/" + key for p in policies
                      for governor_name in ("schedutil", "sched_pixel")
                      for key in ("up_rate_limit_us", "down_rate_limit_us", "rate_limit_us")]
            for path in paths:
                if self.adb.shell("test -r " + path, check=False).returncode == 0:
                    self.saved[path] = self.adb.read(path)
        for key in ("screen_brightness_mode", "screen_brightness", "screen_off_timeout"):
            self.display_saved[key] = self.adb.get_text("settings get system " + key)
        # Save original settings before changes for recovery after an unexpected host exit.
        (self.directory / "controls_before.json").write_text(json.dumps({
            "private": True, "sysfs": self.saved, "display": self.display_saved,
        }, indent=2), encoding="utf-8")
        for policy in policies:
            root = CPU_ROOT + policy + "/"
            if self.policy == "performance":
                maximum = self.adb.read(root + "cpuinfo_max_freq")
                self.adb.write(root + "scaling_max_freq", maximum)
            self.adb.write(root + "scaling_governor", governor)
        if self.policy == "ztt":
            # Save controls exposed by the new governor before starting zTT.
            for policy in policies:
                for key in ("up_rate_limit_us", "down_rate_limit_us", "rate_limit_us"):
                    path = CPU_ROOT + policy + "/schedutil/" + key
                    if path not in self.saved and self.adb.shell(
                            "test -r " + path, check=False).returncode == 0:
                        self.saved[path] = self.adb.read(path)
            (self.directory / "controls_before.json").write_text(json.dumps({
                "private": True, "sysfs": self.saved, "display": self.display_saved,
            }, indent=2), encoding="utf-8")
        for key, value in (("screen_brightness_mode", 0),
                           ("screen_brightness", self.config["environment"]["brightness_raw"]),
                           ("screen_off_timeout", 1800000)):
            self.adb.shell(f"settings put system {key} {value}")
            if self.adb.get_text("settings get system " + key) != str(value):
                raise RuntimeError("Display setting verification failed")

    def foreground(self) -> bool:
        text = self.adb.get_text("dumpsys activity activities")
        return any(self.package + "/" in line for line in text.splitlines()
                   if "topResumedActivity" in line or "mResumedActivity" in line or "ResumedActivity:" in line)

    def readiness_scene_active(self) -> bool:
        return self.flow.thread is not None and self.flow.scene_ready.is_set()

    def ready(self, phase: str) -> None:
        if self.policy == "ztt" and not self.readiness_scene_active():
            raise RuntimeError("zTT readiness requires the active fixed App scene")
        consecutive = 0
        previous_present: dict[str, int] = {}
        selected_layer = None
        require_new_frames = self.policy == "ztt"
        deadline = time.monotonic() + 60
        while consecutive < 3:
            if self.flow.thread is not None:
                self.flow.check()
            if self.ztt and self.ztt.process is not None:
                self.ztt.check()
            if self.background:
                self.background.check()
                self.require_workers()
            good = self.foreground()
            sample: dict[str, Any] = {
                "phase": phase, "foreground": good,
                "sampling_mode": "live-interaction" if require_new_frames else "foreground-only",
            }
            if good and self.policy == "ztt":
                candidates = surfaceflinger_layers(
                    self.adb.get_text("dumpsys SurfaceFlinger --list"), self.package)
                if not candidates:
                    candidates = surfaceflinger_layers(
                        self.adb.get_text("dumpsys SurfaceFlinger"), self.package)
                good = False
                for layer in candidates:
                    raw = self.adb.get_text("dumpsys SurfaceFlinger --latency " + shlex.quote(layer))
                    lines = raw.splitlines()
                    if not lines or not lines[0].strip().isdigit() or int(lines[0]) <= 0:
                        continue
                    frames = [int(cols[1]) for line in lines[1:]
                              if len(cols := line.split()) == 3 and all(c.isdigit() for c in cols)
                              and 0 < int(cols[1]) < 9223372036854775807]
                    frames = sorted(set(frames))
                    if len(frames) >= 2:
                        previous = previous_present.get(layer)
                        advanced = None if previous is None else frames[-1] > previous
                        previous_present[layer] = frames[-1]
                        fps = (len(frames) - 1) * 1e9 / (frames[-1] - frames[0])
                        # The first buffer primes this layer; count only later, advancing samples.
                        if 0 < fps <= 500 and (not require_new_frames or advanced is True):
                            if selected_layer != layer:
                                consecutive = 0
                            selected_layer = layer
                            sample.update(layer=layer, fps=fps, fps_source="surfaceflinger-buffer",
                                          latest_present_ns=frames[-1], frames_advanced=advanced)
                            good = True
                            break
            consecutive = consecutive + 1 if good else 0
            self.readiness.append(dict(sample, valid=good, consecutive=consecutive,
                                       policy_running=bool(self.ztt and self.ztt.process is not None)))
            if time.monotonic() >= deadline:
                raise RuntimeError("Three consecutive valid readiness samples were not obtained")
            if consecutive < 3:
                time.sleep(1)

    def require_workers(self) -> None:
        if not self.workers:
            return
        text = self.adb.get_text("ps -A -o PID,PPID,NAME")
        count = sum(1 for line in text.splitlines() if len(parts := line.split()) >= 3
                    and parts[1] == str(self.background.pid) and "stress-ng" in parts[2])
        if count != self.workers:
            raise RuntimeError(f"Expected {self.workers} live background workers, got {count}")

    def prepare_policy(self) -> None:
        # Attach scx before App launch; zTT and UCLAMP wait for the target App.
        if self.policy in ("pavo", "scx_simple", "scx_lavd"):
            key = "scx_pavo" if self.policy == "pavo" else self.policy
            binary = self.config["binaries"][key]
            self.adb.verify_binary(binary, chroot=self.config["environment"]["chroot"])
            self.scheduler = RemoteProcess(
                self.adb, "chroot " + shlex.quote(self.config["environment"]["chroot"]) +
                " " + shlex.quote(binary["path"]), self.directory, key)
            self.scheduler.start()
            deadline = time.monotonic() + 90
            while self.adb.read(SCX_STATE) != "enabled":
                self.scheduler.check()
                if time.monotonic() >= deadline:
                    raise RuntimeError("sched_ext did not attach")
                time.sleep(0.2)
        elif self.policy == "uclamp_gfx_700":
            from baselines.uclamp_gfx import uclamp_gfx as gfx
            if self.adb.shell("test -x /system/bin/uclampset", check=False).returncode != 0:
                raise RuntimeError("Missing executable: /system/bin/uclampset")
            binary = self.config["binaries"]["uclamp_floor_daemon"]
            self.adb.verify_binary(binary)
            gfx.UCLAMP_DAEMON = binary["path"]
            self.uclamp = gfx.UclampGfxSession(self.adb, self.directory, 700, self.package)
        elif self.policy == "ztt":
            self.ztt = ZttSession(self.config["ztt"], self.directory, self.package, self.adb.serial,
                                  adb=self.adb_executable,
                                  duration_s=DURATION_S)

    def validate_app_inputs(self) -> None:
        app_config = dict(self.config["apps"][self.run["app"]], package=self.package)
        validate_inputs(self.platform, self.run["app"], app_config)

    def prepare_device(self) -> None:
        from perfdog.session import PerfDogSession
        perf = self.config["perfdog"]
        ui_path = Path(perf["ui_config"])
        ui = json.loads(ui_path.read_text(encoding="utf-8-sig"))
        validate_ui_config(ui)
        self.validate_app_inputs()
        self.check_environment()
        self.check_battery()
        self.perfdog = PerfDogSession(
            result_dir=self.directory, export_name=self.directory.parent.name + "_" + self.directory.name,
            app_query=self.app_info[2], package_name=self.package,
            perfdog_root=Path(perf["data_root"]), layout=ui["layout"])
        self.tool_identity = verify_perfdog(Path(perf["executable"]), self.perfdog.gui)
        if self.platform == "oneplus15":
            # On OnePlus, set governors before starting the scheduler.
            self.save_and_set_controls()
            time.sleep(2)
            self.prepare_policy()
        else:
            # On Pixel, start scx before the experiment entry sets governors.
            self.prepare_policy()
            self.save_and_set_controls()
            time.sleep(2)

    def prepare_app(self) -> None:
        app_config = dict(self.config["apps"][self.run["app"]], package=self.package)
        self.flow = AppFlow(self.platform, self.run["app"], self.adb.serial,
                            self.adb_executable, app_config, self.directory)
        time.sleep(3)  # Settling time before App launch.
        self.flow.launch()
        time.sleep(10)
        time.sleep(5)
        if not self.foreground():
            raise RuntimeError("Target App did not remain foreground after launch")
        # Select the App only; zTT's scene/readiness preparation is handled by prepare().
        self.perfdog.prepare()
        self.perfdog.select_app(initialization_wait_s=4)
        self.prepared = True

    def prepare(self) -> None:
        self.prepare_device()
        self.prepare_app()
        if self.ztt:
            self.prepare_ztt_scene()

    def prepare_ztt_scene(self) -> None:
        """Keep readiness outside PerfDog recording and before any zTT transitions."""
        if self.perfdog.started or self.background_prepared:
            raise RuntimeError("zTT scene preparation must precede recording")
        try:
            self.flow.start()
            self.flow.wait_for_scene()
            self.ready("before_background")
            self.start_background_load()
            self.ready("after_background")
            self.ztt.start()
            self.background_prepared = True
        finally:
            (self.directory / "readiness.json").write_text(
                json.dumps(self.readiness, indent=2), encoding="utf-8")

    def start_recording(self) -> float:
        if not self.prepared:
            raise RuntimeError("Prepare the App and policy before recording")
        if self.ztt:
            if not self.background_prepared:
                raise RuntimeError("Complete both zTT readiness checks before recording")
            self.flow.check()
            self.ztt.check()
        self.perfdog.start_recording()
        time.sleep(3)  # Wait 3 seconds after PerfDog starts recording.
        return self.perfdog.recording_started_monotonic_s

    def start_background(self) -> None:
        if not self.perfdog.started or self.perfdog.stopped_at_boundary:
            raise RuntimeError("PerfDog must be recording before background startup")
        if self.ztt:
            if not self.background_prepared:
                raise RuntimeError("zTT background and readiness were not prepared")
            self.ztt.check()
            if self.background:
                self.background.check()
                self.require_workers()
            return
        if self.background_prepared:
            raise RuntimeError("Background startup was already completed")
        if self.uclamp:
            self.uclamp.refresh()
        self.start_background_load()
        self.background_prepared = True

    def start_background_load(self) -> None:
        """Start the same matrixprod command for normal capture or zTT preparation."""
        if self.workers:
            stress = self.config["stress_ng"]["path"]
            self.background = RemoteProcess(self.adb,
                shlex.quote(stress) +
                f" --cpu {self.workers} --cpu-method matrixprod --oom-avoid --metrics-brief --times" +
                f" --timeout {BACKGROUND_TIMEOUT_S}s",
                self.directory, "background")
            self.background_start_requested_monotonic_s = time.monotonic()
            self.background.start()
            time.sleep(3)
            self.background.check()
            self.require_workers()

    def start_interaction(self) -> float:
        if not self.background_prepared:
            raise RuntimeError("Complete background preparation before starting the App scenario")
        if not self.perfdog.started or self.perfdog.stopped_at_boundary:
            raise RuntimeError("PerfDog must be recording before App interaction")
        if self.flow.thread is not None and not self.ztt:
            raise RuntimeError("App interaction was already started")
        if self.ztt:
            self.flow.check()
            self.ztt.mark_measurement_start()
        if self.uclamp:
            self.uclamp.refresh()
        if self.background:
            self.background.check()
            self.require_workers()
        started = time.monotonic()
        self.interaction_started_monotonic_s = started
        self.interaction_rounds_offset = self.flow.rounds
        self.flow.deadline = self.perfdog.recording_started_monotonic_s + DURATION_S
        if not self.ztt:
            self.flow.start()
        return self.interaction_started_monotonic_s

    def check_health(self) -> None:
        self.flow.check()
        if not self.foreground():
            raise RuntimeError("Target App is no longer foreground")
        if self.background:
            self.background.check()
            self.require_workers()
        if self.scheduler:
            self.scheduler.check()
            if self.adb.read(SCX_STATE) != "enabled":
                raise RuntimeError("sched_ext fell back during the window")
        elif self.adb.read(SCX_STATE) != "disabled":
            raise RuntimeError("Unexpected sched_ext attachment in native baseline")
        if self.ztt:
            self.ztt.check()
        if self.uclamp:
            if not self.uclamp._daemon_alive():
                raise RuntimeError("UCLAMP maintainer exited")

    def stop_recording(self) -> None:
        self.perfdog.stop_at_boundary()

    def stop_interaction(self) -> None:
        self.flow.stop()
        self.flow.check()
        if not self.flow.scene_ready.is_set():
            raise RuntimeError("App never completed a scenario or reached continuous playback during capture")

    def export(self) -> dict[str, Any]:
        result = self.perfdog.save_close_and_export()
        result["measurement_tool_version"] = self.tool_identity["version"]
        result["tool_identity"] = self.tool_identity
        result["host_recording_duration_s"] = (
            self.perfdog.recording_stopped_monotonic_s - self.perfdog.recording_started_monotonic_s)
        if self.interaction_started_monotonic_s is None or self.flow.finished_monotonic_s is None:
            raise RuntimeError("App interaction boundary timestamps are missing")
        result["host_interaction_duration_s"] = (
            self.flow.finished_monotonic_s - self.interaction_started_monotonic_s)
        result["recording_lead_s"] = (
            self.interaction_started_monotonic_s - self.perfdog.recording_started_monotonic_s)
        recorded_end = min(self.flow.finished_monotonic_s,
                           self.perfdog.recording_stopped_monotonic_s)
        result["recorded_interaction_duration_s"] = (
            recorded_end - self.interaction_started_monotonic_s)
        result["recording_tail_s"] = self.perfdog.recording_stopped_monotonic_s - recorded_end
        result["interaction_stop_lag_s"] = self.flow.finished_monotonic_s - recorded_end
        result["background_start_requested_monotonic_s"] = self.background_start_requested_monotonic_s
        result["interaction_started_monotonic_s"] = self.interaction_started_monotonic_s
        result["interaction_finished_monotonic_s"] = self.flow.finished_monotonic_s
        result["measurement_target_s"] = DURATION_S
        result["measurement_target_scope"] = "PerfDog recording, including startup lead"
        result["stop_boundary"] = "perfdog-recording-deadline"
        result["interaction_rounds_completed"] = self.flow.rounds - self.interaction_rounds_offset
        if self.ztt:
            result["ztt_readiness_excluded_from_recording"] = True
            result["scene_continues_from_readiness"] = True
        result["background_log_scope"] = "process_lifetime"
        self.exported = True
        return result

    def cleanup(self) -> None:
        errors = []
        actions = []
        if self.perfdog and not self.exported:
            actions.append(self.perfdog.abort)
        if self.flow:
            actions.append(self.flow.stop)
        if self.ztt:
            actions.append(lambda: self.ztt.stop(validate=self.exported))
        if self.uclamp:
            def stop_uclamp():
                result = self.uclamp.stop(best_effort=not self.exported)
                if (not result["restore_readback_verified"]
                        or result["daemon"].get("alive_after_stop")):
                    raise RuntimeError("UCLAMP settings or maintainer were not restored")
            actions.append(stop_uclamp)
        if self.background:
            actions.append(self.background.stop)
        if self.scheduler:
            actions.append(self.scheduler.stop)
        if self.flow:
            actions.append(self.flow.close)
        for action in actions:
            try:
                action()
            except BaseException as exc:
                errors.append(str(exc))
        # Other policies change governors, not the device-managed frequency limits.
        roots = ({p.rsplit("/", 1)[0] for p in self.saved
                  if p.endswith("/scaling_min_freq")}
                 if self.policy in ("ztt", "performance") else set())
        for root in roots:
            try:
                minimum = self.saved[root + "/scaling_min_freq"]
                maximum = self.saved[root + "/scaling_max_freq"]
                current_min = int(self.adb.read(root + "/scaling_min_freq"))
                if current_min > int(maximum):
                    self.adb.write(root + "/scaling_min_freq", minimum)
                self.adb.write(root + "/scaling_max_freq", maximum)
                self.adb.write(root + "/scaling_min_freq", minimum)
            except BaseException as exc:
                errors.append(str(exc))
        # Restore current controls, switch back to the original governor, then restore reappearing nodes.
        deferred = []
        restore_order = sorted(self.saved, key=lambda p: p.endswith("/scaling_governor"))
        for path in restore_order:
            value = self.saved[path]
            if path.endswith(("/scaling_min_freq", "/scaling_max_freq")):
                continue
            try:
                if re.search(r"/(?:schedutil|sched_pixel)/", path) and self.adb.shell(
                        "test -e " + shlex.quote(path), check=False).returncode:
                    deferred.append((path, value))
                    continue
                self.adb.write(path, value)
            except BaseException as exc:
                errors.append(str(exc))
        for path, value in deferred:
            try:
                self.adb.write(path, value)
            except BaseException as exc:
                errors.append(str(exc))
        for key, value in self.display_saved.items():
            try:
                if value == "null":
                    self.adb.shell("settings delete system " + key)
                else:
                    self.adb.shell("settings put system " + key + " " + shlex.quote(value))
                if self.adb.get_text("settings get system " + key) != value:
                    raise RuntimeError("Display restore readback mismatch: " + key)
            except BaseException as exc:
                errors.append(str(exc))
        try:
            if self.scheduler and self.adb.read(SCX_STATE) != "disabled":
                errors.append("sched_ext did not detach")
        except BaseException as exc:
            errors.append("sched_ext detach check failed: " + type(exc).__name__)
        (self.directory / "cleanup.json").write_text(
            json.dumps({"errors": errors, "restored": not errors}, indent=2), encoding="utf-8")
        if errors:
            raise CleanupError("Cleanup failed; stop campaign: " + "; ".join(errors))
