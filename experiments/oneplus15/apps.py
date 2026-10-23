from __future__ import annotations

import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import uiautomator2 as u2

from .helpers.adb_client import AdbClient, AdbError


LOGGER = logging.getLogger("oneplus15_apps")
BOUNDS_PATTERN = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


class AutomationError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


class ArtifactRecorder:
    def __init__(self, root: Path, device: Any) -> None:
        self.root = root
        self.device = device
        self.root.mkdir(parents=True, exist_ok=False)
        self.events_path = self.root / "events.jsonl"

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
        return cleaned.strip("_") or "step"

    def event(self, event_name: str, **details: Any) -> None:
        payload = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event_name,
            **details,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def capture(self, name: str) -> None:
        stem = self._safe_name(name)
        xml_path = self.root / f"{stem}.xml"
        png_path = self.root / f"{stem}.png"
        try:
            xml_path.write_text(self.device.dump_hierarchy(), encoding="utf-8")
        except Exception as exc:  # Capture must not hide the original automation failure.
            self.event("capture_xml_failed", name=name, error=str(exc))
        try:
            self.device.screenshot(str(png_path))
        except Exception as exc:
            self.event("capture_screenshot_failed", name=name, error=str(exc))

    def write_json(self, name: str, payload: Any) -> None:
        (self.root / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class OnePlusAppAutomation:
    def __init__(
        self,
        *,
        serial: str,
        device_config_path: Path,
        apps_config_path: Path,
        artifact_parent: Path,
        set_permissive: bool,
        adb_path: str = "adb",
    ) -> None:
        self.serial = serial
        self.device_config = load_json(device_config_path)
        self.apps_config = load_json(apps_config_path)
        self.apps_config_dir = apps_config_path.parent
        self.adb = AdbClient(serial=serial, adb_path=adb_path)
        self.set_permissive_requested = set_permissive
        self.device: Any | None = None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.artifact_dir = artifact_parent / f"oneplus15_app_{timestamp}"
        self.recorder: ArtifactRecorder | None = None

    def connect(self) -> None:
        if self.adb.get_state() != "device":
            raise AutomationError(f"ADB target {self.serial} is not ready")

        expected_serial = self.device_config["serial"]
        hardware_serial = self.adb.getprop("ro.serialno") or self.adb.getprop(
            "ro.boot.serialno"
        )
        if hardware_serial != expected_serial:
            raise AutomationError(
                f"device hardware serial {hardware_serial or '<empty>'} does not match "
                f"configured OnePlus 15 {expected_serial} (ADB target: {self.serial})"
            )

        if self.set_permissive_requested and self.adb.get_selinux_mode().lower() != "permissive":
            LOGGER.info("Switching the rooted device to SELinux permissive mode")
            self.adb.set_permissive()

        try:
            framework = self.adb.framework_probe()
        except AdbError as exc:
            raise AutomationError(
                "Android framework commands are unavailable. "
                "Check ADB and SELinux setup in env/oneplus15.md."
            ) from exc

        self.device = u2.connect(self.serial)
        self.recorder = ArtifactRecorder(self.artifact_dir, self.device)
        info = self.device.info
        preflight = {
            "serial": self.serial,
            "adb_state": "device",
            "selinux": self.adb.get_selinux_mode(),
            "boot_completed": self.adb.getprop("sys.boot_completed"),
            "model": self.adb.getprop("ro.product.model"),
            "product": self.adb.getprop("ro.build.product"),
            "android_release": self.adb.getprop("ro.build.version.release"),
            "kernel_release": self.adb.shell(["uname", "-r"], timeout=10.0),
            "framework": framework,
            "uiautomator": info,
        }
        self.recorder.write_json("preflight.json", preflight)
        self.recorder.event("connected", **preflight)

        actual_size = (int(info["displayWidth"]), int(info["displayHeight"]))
        expected_size = tuple(self.device_config["display"]["physical_size"])
        if sorted(actual_size) != sorted(expected_size):
            raise AutomationError(
                f"unexpected display size {actual_size}; expected {expected_size}"
            )

    def _require_ready(self) -> tuple[Any, ArtifactRecorder]:
        if self.device is None or self.recorder is None:
            raise AutomationError("automation is not connected")
        return self.device, self.recorder

    def get_app_config(self, app_key: str) -> dict[str, Any]:
        try:
            app = self.apps_config["apps"][app_key]
        except KeyError as exc:
            raise AutomationError(f"unknown app key: {app_key}") from exc
        return app

    def preflight_app(self, app_key: str) -> dict[str, Any]:
        _, recorder = self._require_ready()
        app = self.get_app_config(app_key)
        package = app["package"]
        package_path = self.adb.package_path(package)
        if not package_path.startswith("package:"):
            raise AutomationError(f"package is not installed: {package}")
        result = {
            "key": app_key,
            "name": app["name"],
            "package": package,
            "package_path": package_path,
            **self.adb.package_version(package),
        }
        recorder.write_json(f"app_{app_key}.json", result)
        recorder.event("app_preflight", **result)
        return result

    def start_app(self, package: str, timeout: float) -> float:
        device, recorder = self._require_ready()
        recorder.event("cold_start_begin", package=package)
        device.app_stop(package)
        time.sleep(1.0)
        started = time.perf_counter()
        device.app_start(package, wait=True, stop=False)
        pid = device.app_wait(package, front=True, timeout=timeout)
        elapsed = time.perf_counter() - started
        if not pid:
            raise AutomationError(f"{package} did not become foreground within {timeout:.1f}s")
        current = device.app_current()
        if current.get("package") != package:
            raise AutomationError(f"unexpected foreground package after launch: {current}")
        recorder.event(
            "cold_start_complete",
            package=package,
            pid=pid,
            startup_seconds=elapsed,
            current=current,
        )
        recorder.capture("01_cold_start")
        return elapsed

    def close_app(self, package: str) -> None:
        device, recorder = self._require_ready()
        device.app_stop(package)
        time.sleep(0.5)
        current = device.app_current()
        recorder.event("app_closed", package=package, current=current)
        if current.get("package") == package:
            raise AutomationError("App remained foreground after stop")

    def return_to_activity(self, package: str, activities: list[str], attempts: int = 6) -> None:
        """Re-enter a round without restarting the App or accepting the launcher."""
        device, recorder = self._require_ready()
        for attempt in range(attempts + 1):
            current = device.app_current()
            if current.get("package") != package:
                raise AutomationError("Round reset left the configured foreground App")
            if current.get("activity") in activities:
                recorder.event("round_home_ready", back_presses=attempt)
                return
            if attempt < attempts:
                device.press("back")
                time.sleep(0.8)
        raise AutomationError("Round reset did not reach the configured home activity")

    def _selector_query(self, selector: dict[str, Any]) -> Any:
        device, _ = self._require_ready()
        selector_type = selector["type"]
        value = selector["value"]
        if selector_type == "text":
            return device(text=value)
        if selector_type == "description":
            return device(description=value)
        if selector_type == "resource_id":
            query = {"resourceId": value}
            if "text" in selector:
                query["text"] = selector["text"]
            if "description" in selector:
                query["description"] = selector["description"]
            return device(**query)
        if selector_type == "description_contains":
            return device(descriptionContains=value)
        raise AutomationError(f"unsupported selector type: {selector_type}")

    def _iter_selectors(self, selectors: Iterable[dict[str, Any]]) -> Iterable[tuple[Any, dict[str, Any]]]:
        for selector in selectors:
            query = self._selector_query(selector)
            index = selector.get("index", 0)
            if type(index) is not int or index < 0:
                raise AutomationError("Selector index must be a nonnegative integer")
            yield (query[index] if index else query, selector)

    def _visible_query_center(self, query: Any) -> tuple[int, int] | None:
        device, _ = self._require_ready()
        bounds = query.info["bounds"]
        width, height = device.window_size()
        left, top, right, bottom = (bounds[key] for key in ("left", "top", "right", "bottom"))
        if 0 <= left < right <= width and 0 <= top < bottom <= height:
            return (left + right) // 2, (top + bottom) // 2
        return None

    def wait_for_any(
        self, selectors: list[dict[str, Any]], timeout: float, *, visible: bool = False,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for query, selector in self._iter_selectors(selectors):
                if query.exists:
                    if visible and self._visible_query_center(query) is None:
                        continue
                    return selector
            time.sleep(0.25)
        raise AutomationError(f"none of the selectors appeared within {timeout:.1f}s: {selectors}")

    def click_first(
        self,
        *,
        step: str,
        selectors: list[dict[str, Any]],
        fallback: list[float] | None,
        timeout: float,
        native_tap: bool = False,
    ) -> None:
        device, recorder = self._require_ready()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for query, selector in self._iter_selectors(selectors):
                if query.exists:
                    if native_tap:
                        point = self._visible_query_center(query)
                        if point is None:
                            continue
                        self.adb.shell(["input", "tap", str(point[0]), str(point[1])])
                        recorder.event("click", step=step, method="native_selector",
                                       selector=selector, absolute=list(point))
                        return
                    query.click()
                    recorder.event("click", step=step, method="selector", selector=selector)
                    return
            time.sleep(0.25)

        if fallback is None:
            recorder.capture(f"failure_{step}")
            raise AutomationError(f"no selector found for {step}: {selectors}")
        width, height = device.window_size()
        x = int(width * float(fallback[0]))
        y = int(height * float(fallback[1]))
        device.click(x, y)
        recorder.event(
            "click",
            step=step,
            method="normalized_fallback",
            normalized=fallback,
            absolute=[x, y],
        )

    @staticmethod
    def _bounds_center(bounds: str) -> tuple[int, int]:
        match = BOUNDS_PATTERN.fullmatch(bounds)
        if not match:
            raise AutomationError(f"invalid UI bounds: {bounds}")
        left, top, right, bottom = map(int, match.groups())
        return (left + right) // 2, (top + bottom) // 2

    def click_matching_ancestor(
        self,
        *,
        step: str,
        node_resource_id: str,
        ancestor_resource_id: str,
        fallback: list[float] | None,
        timeout: float = 0.0,
        index: int = 0,
        native_tap: bool = False,
    ) -> None:
        if type(index) is not int or index < 0:
            raise ValueError("Video index must be a nonnegative integer")
        device, recorder = self._require_ready()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            root = ET.fromstring(device.dump_hierarchy())
            parents = {child: parent for parent in root.iter() for child in parent}
            seen = set()
            matches = []
            for node in root.iter():
                if node.get("resource-id") != node_resource_id:
                    continue
                current = node
                while current in parents:
                    current = parents[current]
                    if current.get("resource-id") == ancestor_resource_id:
                        if current in seen:
                            break
                        seen.add(current)
                        matches.append(current)
                        break
            if index < len(matches):
                current = matches[index]
                x, y = self._bounds_center(current.get("bounds", ""))
                width, height = device.window_size()
                if current.get("visible-to-user") == "true" and 0 <= x < width and 0 <= y < height:
                    if native_tap:
                        self.adb.shell(["input", "tap", str(x), str(y)])
                    else:
                        device.click(x, y)
                    recorder.event(
                        "click", step=step,
                        method="native_hierarchy_ancestor" if native_tap else "hierarchy_ancestor",
                        node_resource_id=node_resource_id,
                        ancestor_resource_id=ancestor_resource_id,
                        absolute=[x, y],
                    )
                    return
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)

        if fallback is None:
            recorder.capture(f"failure_{step}")
            raise AutomationError(f"no matching hierarchy ancestor found for {step}")
        width, height = device.window_size()
        x = int(width * float(fallback[0]))
        y = int(height * float(fallback[1]))
        device.click(x, y)
        recorder.event(
            "click",
            step=step,
            method="normalized_fallback",
            normalized=fallback,
            absolute=[x, y],
        )

    def return_to_home_navigation(
        self,
        package: str,
        selectors: list[dict[str, Any]],
        *,
        attempts: int = 4,
    ) -> None:
        device, recorder = self._require_ready()
        skipped = self.click_hierarchy_text(
            step="douyin_startup_ad_skip", text="跳过广告",
            timeout=2.0, required=False,
        )
        if skipped:
            time.sleep(1.5)
        for attempt in range(attempts + 1):
            if device.app_current().get("package") != package:
                raise AutomationError("Round reset left the configured foreground App")
            for query, _ in self._iter_selectors(selectors):
                if query.exists:
                    recorder.event("home_navigation_ready", back_presses=attempt)
                    return
            if attempt < attempts:
                device.press("back")
                time.sleep(0.8)
        recorder.capture("failure_home_navigation")
        raise AutomationError("Douyin bottom navigation did not become available")

    def press_back_until(
        self,
        *,
        step: str,
        selectors: list[dict[str, Any]],
        attempts: int,
    ) -> None:
        device, recorder = self._require_ready()
        for attempt in range(attempts + 1):
            for query, _ in self._iter_selectors(selectors):
                if query.exists:
                    recorder.event(step, back_presses=attempt)
                    return
            if attempt < attempts:
                device.press("back")
                time.sleep(0.8)
        recorder.capture(f"failure_{step}")
        raise AutomationError(f"target UI state was not reached for {step}")

    def swipe_normalized(self, gesture: dict[str, Any]) -> None:
        device, recorder = self._require_ready()
        width, height = device.window_size()
        start = gesture["start"]
        end = gesture["end"]
        absolute = [
            int(width * float(start[0])),
            int(height * float(start[1])),
            int(width * float(end[0])),
            int(height * float(end[1])),
        ]
        device.swipe(*absolute, duration=float(gesture["duration_seconds"]))
        recorder.event("swipe", normalized=[start, end], absolute=absolute)

    def click_normalized(
        self, *, step: str, normalized: list[float], native_tap: bool = False,
    ) -> None:
        device, recorder = self._require_ready()
        width, height = device.window_size()
        x = int(width * float(normalized[0]))
        y = int(height * float(normalized[1]))
        if native_tap:
            self.adb.shell(["input", "tap", str(x), str(y)])
        else:
            device.click(x, y)
        recorder.event(
            "click",
            step=step,
            method="native_normalized_coordinate" if native_tap else "normalized_game_coordinate",
            normalized=normalized,
            absolute=[x, y],
        )

    def wait_for_hierarchy_node(
        self,
        *,
        timeout: float,
        text: str | None = None,
        resource_id: str | None = None,
        contains_text: bool = False,
    ) -> dict[str, str]:
        device, recorder = self._require_ready()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            root = ET.fromstring(device.dump_hierarchy())
            for node in root.iter("node"):
                node_text = node.get("text", "")
                text_matches = text is None or (
                    text in node_text if contains_text else node_text == text
                )
                id_matches = resource_id is None or node.get("resource-id") == resource_id
                if text_matches and id_matches:
                    attributes = dict(node.attrib)
                    recorder.event(
                        "hierarchy_node_ready",
                        text=text,
                        resource_id=resource_id,
                        attributes=attributes,
                    )
                    return attributes
            time.sleep(0.25)
        raise AutomationError(
            f"hierarchy node did not appear within {timeout:.1f}s: "
            f"text={text!r}, resource_id={resource_id!r}"
        )

    def click_hierarchy_text(
        self,
        *,
        step: str,
        text: str,
        timeout: float,
        required: bool = True,
    ) -> bool:
        device, recorder = self._require_ready()
        try:
            node = self.wait_for_hierarchy_node(timeout=timeout, text=text)
        except AutomationError:
            if required:
                recorder.capture(f"failure_{step}")
                raise
            recorder.event("optional_hierarchy_text_absent", step=step, text=text)
            return False

        x, y = self._bounds_center(node.get("bounds", ""))
        device.click(x, y)
        recorder.event(
            "click",
            step=step,
            method="hierarchy_text",
            text=text,
            absolute=[x, y],
        )
        return True

    def wait_for_activity(
        self,
        *,
        package: str,
        activities: list[str],
        timeout: float,
    ) -> dict[str, Any]:
        device, recorder = self._require_ready()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = device.app_current()
            if current.get("package") == package and current.get("activity") in activities:
                recorder.event("activity_ready", current=current)
                return current
            time.sleep(0.25)
        recorder.capture("failure_activity_transition")
        raise AutomationError(
            f"expected {package} activity in {activities}, current={device.app_current()}"
        )
    def wait_for_landscape(self, timeout: float) -> tuple[int, int]:
        device, recorder = self._require_ready()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            size = device.window_size()
            if size[0] > size[1]:
                recorder.event("landscape_ready", window_size=list(size))
                return size
            time.sleep(0.25)
        recorder.capture("failure_landscape")
        raise AutomationError(f"landscape mode did not appear within {timeout:.1f}s")

    def wait_for_portrait(self, timeout: float) -> tuple[int, int]:
        device, recorder = self._require_ready()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            size = device.window_size()
            if size[1] > size[0]:
                recorder.event("portrait_ready", window_size=list(size))
                return size
            time.sleep(0.25)
        recorder.capture("failure_portrait")
        raise AutomationError(f"portrait mode did not appear within {timeout:.1f}s")

    def run_douyin_profile_video(self, app: dict[str, Any], swipes: int) -> int:
        device, recorder = self._require_ready()
        flow = app["flows"]["profile_video"]
        timeout = float(flow["selector_timeout_seconds"])

        self.return_to_home_navigation(app["package"], flow["home_navigation_selectors"])
        self.click_first(
            step="douyin_me_tab",
            selectors=flow["me_tab"]["selectors"],
            fallback=flow["me_tab"]["fallback_normalized"],
            timeout=timeout,
            native_tap=True,
        )
        self.wait_for_any(flow["own_profile_marker"], timeout, visible=True)
        recorder.capture("02_own_profile")

        self.click_first(
            step="douyin_following_count",
            selectors=flow["following_count"]["selectors"],
            fallback=None,
            timeout=timeout,
            native_tap=True,
        )
        self.wait_for_any(flow["following_list_marker"], timeout, visible=True)
        recorder.capture("03_following_list")

        self.click_first(
            step="douyin_target_account",
            selectors=flow["target_account"]["selectors"],
            fallback=None,
            timeout=timeout,
            native_tap=True,
        )
        try:
            self.wait_for_any(flow["target_profile_marker"], 3.0, visible=True)
        except AutomationError:
            recorder.event("douyin_target_account_retry")
            self.click_first(
                step="douyin_target_account_retry",
                selectors=flow["target_account"]["selectors"],
                fallback=None,
                timeout=timeout,
                native_tap=True,
            )
            self.wait_for_any(flow["target_profile_marker"], timeout, visible=True)
        recorder.capture("04_target_profile")

        video = flow["target_video"]
        self.click_matching_ancestor(
            step="douyin_target_video",
            node_resource_id=video["node_resource_id"],
            ancestor_resource_id=video["ancestor_resource_id"],
            fallback=None,
            timeout=timeout,
            index=video["index"],
            native_tap=True,
        )
        self.wait_for_any(flow["video_marker"], timeout)
        recorder.capture("05_video_open")

        completed = 0
        for index in range(swipes):
            self.swipe_normalized(flow["swipe"])
            completed += 1
            recorder.event("douyin_swipe_complete", index=index + 1, total=swipes)
            time.sleep(float(flow["between_swipes_seconds"]))

        recorder.capture("06_after_swipes")
        current = device.app_current()
        if current.get("package") != app["package"]:
            raise AutomationError(f"Douyin left foreground during interaction: {current}")
        self.return_to_home_navigation(app["package"], flow["home_navigation_selectors"])
        return completed


    def click_weibo_profile(self, selectors: list[dict[str, Any]], timeout: float) -> None:
        _, recorder = self._require_ready()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for profile, selector in self._iter_selectors(selectors):
                if not profile.exists:
                    continue
                # Search-box text has no avatar sibling; the account card does.
                avatar = profile.sibling(className="android.widget.FrameLayout")
                if not avatar.exists:
                    continue
                point = self._visible_query_center(avatar)
                if point is None:
                    continue
                self.adb.shell(["input", "tap", str(point[0]), str(point[1])])
                recorder.event("click", step="weibo_target_profile",
                               method="native_profile_avatar", selector=selector,
                               absolute=list(point))
                return
            time.sleep(0.25)
        recorder.capture("failure_weibo_target_profile")
        raise AutomationError("The configured Weibo account card was not found")

    def run_weibo_profile_feed(self, app: dict[str, Any], swipes: int) -> int:
        device, recorder = self._require_ready()
        flow = app["flows"]["profile_feed"]
        timeout = float(flow["activity_timeout_seconds"])
        activities = flow["activities"]

        self.return_to_activity(app["package"], activities["home"])

        self.wait_for_activity(
            package=app["package"],
            activities=activities["home"],
            timeout=timeout,
        )
        time.sleep(float(flow["initial_settle_seconds"]))
        self.click_normalized(step="weibo_discover_tab", normalized=flow["discover_tab"])
        time.sleep(float(flow["between_navigation_seconds"]))
        recorder.capture("02_weibo_discover")

        self.click_normalized(step="weibo_search_box", normalized=flow["search_box"], native_tap=True)
        self.wait_for_activity(
            package=app["package"],
            activities=activities["search_middle"],
            timeout=timeout,
        )
        time.sleep(float(flow["between_navigation_seconds"]))
        recorder.capture("03_weibo_search_history")

        self.click_first(
            step="weibo_search_history",
            selectors=flow["search_history_selectors"],
            fallback=None,
            timeout=timeout,
            native_tap=True,
        )
        self.wait_for_activity(
            package=app["package"],
            activities=activities["search_results"],
            timeout=timeout,
        )
        time.sleep(float(flow["between_navigation_seconds"]))
        recorder.capture("04_weibo_search_results")

        self.click_weibo_profile(flow["target_profile_selectors"], timeout)
        self.wait_for_activity(
            package=app["package"],
            activities=activities["profile"],
            timeout=timeout,
        )
        time.sleep(float(flow["between_navigation_seconds"]))
        recorder.capture("05_weibo_target_profile")

        completed = 0
        for index in range(swipes):
            self.swipe_normalized(flow["swipe"])
            completed += 1
            recorder.event("weibo_swipe_complete", index=index + 1, total=swipes)
            time.sleep(float(flow["between_swipes_seconds"]))

        recorder.capture("06_weibo_after_swipes")
        current = device.app_current()
        if current.get("package") != app["package"]:
            raise AutomationError(f"Weibo left foreground during interaction: {current}")
        self.return_to_activity(app["package"], activities["home"])
        return completed





    def write_adb_history(self) -> None:
        if self.recorder is not None:
            self.adb.write_history(self.recorder.root / "adb_commands.json")
