"""Run the selected App flow and request its exit when capture ends."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from app_checks import validate_pixel_config, validate_oneplus_config, require_concrete

def validate_inputs(platform: str, app: str, config: dict) -> None:
    keys = ("ui_config",) if platform == "pixel6" else ("apps_config", "device_config")
    loaded = {}
    for key in keys:
        path = Path(config[key])
        loaded[key] = json.loads(path.read_text(encoding="utf-8-sig"))
    if platform == "pixel6":
        validate_pixel_config(loaded["ui_config"], app, config["package"])
    else:
        validate_oneplus_config(loaded["apps_config"], app, config["package"])
        device = loaded["device_config"]
        require_concrete(device)
        if not isinstance(device.get("serial"), str):
            raise ValueError("Configure the hardware serial privately")
        size = device["display"]["physical_size"]
        if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
            raise ValueError("Configure the actual OnePlus display size")


class FlowStopped(BaseException):
    """End the interaction thread when capture finishes; distinguish normal stop from App failure."""


class InterruptibleTime:
    def __init__(self, stop: threading.Event, scene_ready: threading.Event | None = None):
        self.stop = stop
        self.scene_ready = scene_ready

    def sleep(self, seconds: float) -> None:
        if seconds >= 1_000_000 and self.scene_ready is not None:
            self.scene_ready.set()  # Continuous playback has reached its dwell.
        if self.stop.wait(seconds):
            raise FlowStopped()

    def __getattr__(self, name: str) -> Any:
        return getattr(time, name)


class AppFlow:
    def __init__(self, platform: str, app: str, serial: str, adb: str,
                 config: dict[str, Any], directory: Path):
        self.platform, self.app = platform, app
        self.stop_event = threading.Event()
        self.scene_ready = threading.Event()
        self.deadline = None
        self.finished_monotonic_s = None
        self.error: BaseException | None = None
        self.thread: threading.Thread | None = None
        self.rounds = 0
        self.time_originals = []
        self.automation = None
        self.started = False
        self.package = config["package"]
        validate_inputs(platform, app, config)
        proxy = InterruptibleTime(self.stop_event, self.scene_ready)
        if platform == "pixel6":
            from pixel6 import apps as app_tester
            self.module = app_tester
            path = Path(config["ui_config"])
            self.automation = app_tester.AppTester(
                serial=serial, app_config=json.loads(path.read_text(encoding="utf-8-sig")))
            original_wait = self.automation._wait_or_stop
            def wait_or_stop(seconds):
                if app == "aqy" and seconds >= 1_000_000:
                    self.scene_ready.set()
                return original_wait(seconds)
            self.automation._wait_or_stop = wait_or_stop
            self.run_round = lambda: self.automation.run_test(
                app, 1_000_000 if app == "aqy" else 6)
            self.time_originals.append((app_tester, app_tester.time))
            app_tester.time = proxy
        else:
            import oneplus15.apps as app_automation
            import oneplus15.helpers.app_flows as helpers
            self.module = app_automation
            self.automation = app_automation.OnePlusAppAutomation(
                serial=serial, adb_path=adb,
                device_config_path=Path(config["device_config"]),
                apps_config_path=Path(config["apps_config"]),
                artifact_parent=directory / "interaction",
                set_permissive=False)
            self.automation.connect()
            self.app_config = self.automation.get_app_config(app)
            if self.app_config["package"] != self.package:
                raise RuntimeError("App-flow package differs from the experiment target")
            if app == "aqy":
                self.app_config = helpers.apply_video_config(self.app_config)
            methods = {
                "dy": lambda: self.automation.run_douyin_profile_video(self.app_config, 6),
                "ks": lambda: helpers.run_kuaishou_profile_video_stable(self.automation, self.app_config, 6),
                "tb": lambda: helpers.run_taobao_feed(self.automation, self.app_config, 6),
                "wb": lambda: self.automation.run_weibo_profile_feed(self.app_config, 6),
                "aqy": lambda: helpers.run_iqiyi_playback(self.automation, self.app_config, 1_000_000),
            }
            self.run_round = methods[app]
            for module in (app_automation, helpers):
                self.time_originals.append((module, module.time))
                module.time = proxy

    def launch(self) -> None:
        if self.platform == "pixel6":
            success, _ = self.automation.start_app(self.app)
            if not success:
                raise RuntimeError("App launch failed")
            self.automation.set_stop_event(self.stop_event)
            self.automation.set_repeated_scenario_mode(self.app != "aqy")
        else:
            self.automation.preflight_app(self.app)
            self.automation.start_app(self.package, timeout=30)
        self.started = True

    def start(self) -> None:
        def worker() -> None:
            try:
                while not self.stop_event.is_set():
                    if self.deadline is not None and time.monotonic() >= self.deadline:
                        self.stop_event.set()
                        break
                    result = self.run_round()
                    if self.stop_event.is_set():
                        break
                    if result is False or (isinstance(result, int) and not isinstance(result, bool) and result <= 0):
                        raise RuntimeError("App flow failed")
                    self.rounds += 1
                    if self.app != "aqy":
                        self.scene_ready.set()
                    if self.app == "aqy" and not self.stop_event.is_set():
                        raise RuntimeError("Continuous playback ended before the deadline")
            except FlowStopped:
                pass
            except BaseException as exc:
                self.error = exc
            finally:
                self.finished_monotonic_s = time.monotonic()
        self.thread = threading.Thread(target=worker, name="pavo-app-flow", daemon=True)
        self.thread.start()

    def check(self) -> None:
        if self.error is not None:
            raise RuntimeError(f"App interaction failed: {self.error}")
        if not self.stop_event.is_set() and (self.thread is None or not self.thread.is_alive()):
            raise RuntimeError("App interaction stopped early")

    def wait_for_scene(self, timeout_s: float = 180) -> None:
        deadline = time.monotonic() + timeout_s
        while not self.scene_ready.wait(0.1):
            self.check()
            if time.monotonic() >= deadline:
                raise RuntimeError("App did not complete an initial scenario or reach continuous playback")
        self.check()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError("App flow did not stop; abort the campaign")

    def close(self) -> None:
        try:
            self.stop()
        finally:
            for module, original in self.time_originals:
                module.time = original
            # Do not close an App while an uninterruptible interaction is still acting on it.
            if self.started and not (self.thread and self.thread.is_alive()):
                result = self.automation.close_app(self.app if self.platform == "pixel6" else self.package)
                if result is False:
                    raise RuntimeError("App cleanup failed")
                self.started = False
