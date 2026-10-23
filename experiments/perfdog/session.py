#!/usr/bin/env python3
"""Select the App, control PerfDog recording and copy untouched exports.

Pixel 6 and OnePlus share this controller.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

import pyperclip

from .controller import PerfDogController


def describe_perfdog_export(path: Path) -> dict[str, Any]:
    """Record export paths and file sizes."""
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"PerfDog did not produce a nonempty raw export: {path}")
    return {"path": str(path.resolve()), "bytes": path.stat().st_size}



class PerfDogSession:
    """One capture: select App, start, stop and export."""

    def __init__(
        self,
        *,
        result_dir: Path,
        export_name: str,
        app_query: str,
        package_name: str,
        perfdog_root: Path,
        layout: dict,
    ) -> None:
        self.result_dir = result_dir
        self.export_name = export_name
        self.app_query = app_query
        self.package_name = package_name
        self.perfdog_root = perfdog_root
        self.data_root = (
            perfdog_root / "data" if (perfdog_root / "data").is_dir() else perfdog_root
        )
        self.gui = PerfDogController(layout)
        self.prepared = False
        self.selected = False
        self.selection_attempted = False
        self.started = False
        self.stopped_at_boundary = False
        self.saved = False
        self.closed = False
        self.recording_started_monotonic_s: float | None = None
        self.recording_stopped_monotonic_s: float | None = None
        self.recording_started_epoch_s: float | None = None
        self.recording_stopped_epoch_s: float | None = None
        self.export_source: Path | None = None
        self.export_copy: Path | None = None
        self.prepared_screenshot: Path | None = None
        self.selected_screenshot: Path | None = None
        self.prepared_window_hwnd: int | None = None
        self.selection_completed_epoch_s: float | None = None

    def prepare(self) -> None:
        """Open and search the App list without selecting the result yet."""
        if not self.perfdog_root.is_dir():
            raise RuntimeError(f"PerfDog root does not exist: {self.perfdog_root}")
        window = self.gui.focus_window()
        self.prepared_window_hwnd = int(window["hwnd"])
        self.gui.click(self.gui.SELECT_APP)
        time.sleep(2)
        self.gui.click(self.gui.SEARCH_BOX)
        time.sleep(1)
        self.gui.mouse.hotkey("ctrl", "a")
        self.gui.mouse.press("delete")
        previous_clipboard = pyperclip.paste()
        try:
            pyperclip.copy(self.app_query)
            self.gui.mouse.hotkey("ctrl", "v")
            time.sleep(0.25)
        finally:
            pyperclip.copy(previous_clipboard)
        # The wireless device list can take several seconds to refresh.
        time.sleep(8)
        self.prepared_screenshot = self.result_dir / "perfdog_prepared.png"
        self.gui.mouse.screenshot(str(self.prepared_screenshot))
        self.prepared = True

    def cancel_prepare(self) -> None:
        if not self.prepared or self.started:
            return
        if self.prepared_window_hwnd is not None:
            self.gui.assert_foreground(self.prepared_window_hwnd)
        self.gui.mouse.press("esc")
        time.sleep(1)
        self.prepared = False

    def select_app(self, *, initialization_wait_s: float = 12.0) -> None:
        """Select the App and wait for PerfDog service initialization."""
        if not self.prepared:
            raise RuntimeError("PerfDog app selection was not prepared")
        if self.prepared_window_hwnd is None:
            raise RuntimeError("PerfDog prepared-window handle is unavailable")
        if initialization_wait_s < 4.0:
            raise ValueError("PerfDog initialization wait must be at least 4 seconds")
        self.gui.assert_foreground(self.prepared_window_hwnd)
        self.selection_attempted = True
        self.gui.click(self.gui.APP_ROW)
        time.sleep(initialization_wait_s)
        window = self.gui.focus_window()
        if int(window["hwnd"]) != self.prepared_window_hwnd:
            raise RuntimeError("PerfDog window changed during App initialization")
        self.selection_completed_epoch_s = time.time()
        self.selected_screenshot = self.result_dir / "perfdog_selected.png"
        self.gui.mouse.screenshot(str(self.selected_screenshot))
        self.selected = True

    def start_recording(self) -> None:
        if not self.selected:
            raise RuntimeError("PerfDog App was not selected")
        if self.prepared_window_hwnd is None:
            raise RuntimeError("PerfDog prepared-window handle is unavailable")
        window = self.gui.focus_window()
        if int(window["hwnd"]) != self.prepared_window_hwnd:
            raise RuntimeError("PerfDog window changed before recording")
        self.gui.click_boundary(self.gui.RECORD_BUTTON, self.prepared_window_hwnd)
        self.recording_started_epoch_s = time.time()
        self.recording_started_monotonic_s = time.monotonic()
        self.started = True

    def start(self) -> None:
        """Start recording after selecting the App."""
        if not self.selected:
            self.select_app()
        self.start_recording()

    def stop_at_boundary(self) -> None:
        if not self.started:
            raise RuntimeError("PerfDog recording was not started")
        if self.stopped_at_boundary:
            return
        if self.prepared_window_hwnd is None:
            raise RuntimeError("PerfDog prepared-window handle is unavailable")
        self.gui.click_boundary(self.gui.RECORD_BUTTON, self.prepared_window_hwnd)
        self.recording_stopped_epoch_s = time.time()
        self.recording_stopped_monotonic_s = time.monotonic()
        self.stopped_at_boundary = True

    def _find_and_copy_export(self, timeout_s: float = 30.0) -> Path:
        if self.recording_started_epoch_s is None:
            raise RuntimeError("PerfDog recording start time is unavailable")
        package_root = self.data_root / self.package_name
        deadline = time.monotonic() + timeout_s
        previous: tuple[Path, int] | None = None
        while time.monotonic() <= deadline:
            candidates: list[Path] = []
            if package_root.is_dir():
                for path in package_root.rglob("*.xlsx"):
                    try:
                        if (
                            path.name.startswith(self.export_name)
                            and path.stat().st_mtime >= self.recording_started_epoch_s - 5.0
                        ):
                            candidates.append(path)
                    except OSError:
                        continue
            if candidates:
                source = max(candidates, key=lambda item: item.stat().st_mtime)
                current = (source, source.stat().st_size)
                if current[1] > 0 and current == previous:
                    destination = self.result_dir / "perfdog_export.xlsx"
                    shutil.copy2(source, destination)
                    self.export_source = source
                    self.export_copy = destination
                    return destination
                previous = current
            time.sleep(1)
        raise RuntimeError(
            f"PerfDog export {self.export_name!r} was not found under {package_root}"
        )

    def save_close_and_export(self) -> dict[str, Any]:
        if not self.stopped_at_boundary:
            self.stop_at_boundary()
        if self.prepared_window_hwnd is None:
            raise RuntimeError("PerfDog prepared-window handle is unavailable")

        window = self.gui.focus_window()
        if int(window["hwnd"]) != self.prepared_window_hwnd:
            raise RuntimeError("PerfDog window changed before export")
        self.gui.click(self.gui.SAVE_BUTTON)
        time.sleep(1)
        self.gui.click(self.gui.EXPORT_NAME)
        self.gui.mouse.hotkey("ctrl", "a")
        self.gui.mouse.press("delete")
        previous_clipboard = pyperclip.paste()
        try:
            pyperclip.copy(self.export_name)
            self.gui.mouse.hotkey("ctrl", "v")
            time.sleep(0.25)
        finally:
            pyperclip.copy(previous_clipboard)
        time.sleep(0.5)
        self.gui.click(self.gui.EXPORT_CONFIRM)
        time.sleep(3)
        self.saved = True

        window = self.gui.focus_window()
        if int(window["hwnd"]) != self.prepared_window_hwnd:
            raise RuntimeError("PerfDog window changed before test close")
        self.gui.click(self.gui.CLOSE_TEST)
        time.sleep(1)
        self.closed = True

        workbook = self._find_and_copy_export()
        if (
            self.recording_started_epoch_s is None
            or self.recording_stopped_epoch_s is None
        ):
            raise RuntimeError("PerfDog recording timestamps are unavailable")
        host_duration_s = (
            self.recording_stopped_monotonic_s - self.recording_started_monotonic_s
        )
        raw_export = describe_perfdog_export(workbook)
        return {
            "capture_complete": True,
            "prepared": self.prepared,
            "selected": self.selected,
            "recorded": self.started,
            "stopped_at_boundary": self.stopped_at_boundary,
            "saved": self.saved,
            "closed": self.closed,
            "prepared_screenshot": str(self.prepared_screenshot),
            "selected_screenshot": str(self.selected_screenshot),
            "export_name": self.export_name,
            "source_xlsx": str(self.export_source),
            "copied_xlsx": str(self.export_copy),
            "recording_started_epoch_s": self.recording_started_epoch_s,
            "recording_stopped_epoch_s": self.recording_stopped_epoch_s,
            "selection_completed_epoch_s": self.selection_completed_epoch_s,
            "host_recording_duration_s": host_duration_s,
            "raw_export": raw_export,
        }

    def abort(self) -> None:
        """Attempt to stop recording and exit the App; return cleanup errors together."""
        try:
            if self.prepared and not self.selection_attempted and not self.started:
                self.cancel_prepare()
            if self.started and not self.stopped_at_boundary:
                self.gui.focus_window()
                self.stop_at_boundary()
            if self.stopped_at_boundary and not self.saved:
                self.gui.mouse.press("esc")
                time.sleep(1)
            if self.selection_attempted and not self.closed:
                self.gui.focus_window()
                self.gui.click(self.gui.CLOSE_TEST)
                time.sleep(1)
                self.closed = True
        except Exception as exc:
            raise RuntimeError("PerfDog cleanup failed; stop the campaign") from exc
