#!/usr/bin/env python3
"""PerfDog window and mouse control using locally configured coordinates."""

from __future__ import annotations

import ctypes
import logging
import time
from dataclasses import dataclass
from typing import Any

import pyautogui
import pygetwindow


LOGGER = logging.getLogger("perfdog_controller")


@dataclass(frozen=True)
class Point:
    x: int
    y: int


class PerfDogController:
    """Check window geometry before clicking to avoid the wrong target."""

    SCREEN_SIZE = (3840, 2160)
    WINDOW_BOUNDS = (1069, 536, 1702, 1016)
    BOUNDS_TOLERANCE_PX = 8

    SELECT_APP = Point(1250, 840)
    SEARCH_BOX = Point(1250, 900)
    APP_ROW = Point(1250, 965)
    RECORD_BUTTON = Point(2694, 663)
    SAVE_BUTTON = Point(2046, 1048)
    EXPORT_NAME = Point(1877, 985)
    EXPORT_CONFIRM = Point(1850, 1187)
    CLOSE_TEST = Point(1390, 630)

    def __init__(self, layout: dict | None = None) -> None:
        if layout is not None:
            self.SCREEN_SIZE = tuple(layout["screen_size"])
            self.WINDOW_BOUNDS = tuple(layout["window_bounds"])
            for key in ("SELECT_APP", "SEARCH_BOX", "APP_ROW", "RECORD_BUTTON",
                        "SAVE_BUTTON", "EXPORT_NAME", "EXPORT_CONFIRM", "CLOSE_TEST"):
                x, y = layout["points"][key]
                setattr(self, key, Point(int(x), int(y)))
        self.mouse = pyautogui
        self.mouse.PAUSE = 0.5
        self.mouse.FAILSAFE = True

    @staticmethod
    def _window_payload(window: Any) -> dict[str, Any]:
        return {
            "title": window.title,
            "hwnd": int(window._hWnd),
            "left": int(window.left),
            "top": int(window.top),
            "width": int(window.width),
            "height": int(window.height),
            "minimized": bool(window.isMinimized),
            "maximized": bool(window.isMaximized),
        }

    def _find_window(self) -> Any:
        windows = [
            window
            for window in pygetwindow.getAllWindows()
            if window.title.startswith("PerfDog(v")
            and window.width > 0
            and window.height > 0
        ]
        if not windows:
            raise RuntimeError("PerfDog application window was not found")
        return max(windows, key=lambda item: item.width * item.height)

    def _assert_geometry(self, window: Any) -> None:
        screen = self.mouse.size()
        actual_screen = (int(screen.width), int(screen.height))
        if actual_screen != self.SCREEN_SIZE:
            raise RuntimeError(
                f"unexpected desktop size: {actual_screen} != {self.SCREEN_SIZE}"
            )

        actual = (
            int(window.left),
            int(window.top),
            int(window.width),
            int(window.height),
        )
        differences = [
            abs(value - expected)
            for value, expected in zip(actual, self.WINDOW_BOUNDS)
        ]
        if any(delta > self.BOUNDS_TOLERANCE_PX for delta in differences):
            raise RuntimeError(
                "PerfDog window geometry changed: "
                f"actual={actual}, expected={self.WINDOW_BOUNDS}, "
                f"tolerance={self.BOUNDS_TOLERANCE_PX}px"
            )
        if window.isMaximized:
            raise RuntimeError("PerfDog must remain in the frozen windowed layout")

    def focus_window(self) -> dict[str, Any]:
        """Focus PerfDog without changing its position or size."""
        window = self._find_window()
        if window.isMinimized:
            window.restore()
            time.sleep(1)
        self._assert_geometry(window)

        hwnd = int(window._hWnd)
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        for attempt in range(1, 4):
            try:
                window.activate()
            except Exception as exc:
                LOGGER.warning("PerfDog activate attempt %d failed: %s", attempt, exc)

            current_thread = kernel32.GetCurrentThreadId()
            foreground = user32.GetForegroundWindow()
            thread_ids = {
                user32.GetWindowThreadProcessId(foreground, None),
                user32.GetWindowThreadProcessId(hwnd, None),
            }
            attached: list[int] = []
            try:
                for thread_id in thread_ids:
                    if thread_id and thread_id != current_thread:
                        if user32.AttachThreadInput(current_thread, thread_id, True):
                            attached.append(thread_id)
                user32.ShowWindow(hwnd, 9)
                user32.BringWindowToTop(hwnd)
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003)
                user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003)
                user32.SetForegroundWindow(hwnd)
                user32.SetActiveWindow(hwnd)
                user32.SetFocus(hwnd)
            finally:
                for thread_id in reversed(attached):
                    user32.AttachThreadInput(current_thread, thread_id, False)
            time.sleep(1)
            if int(user32.GetForegroundWindow()) == hwnd:
                break
            self.mouse.press("alt")
            time.sleep(0.5)
        else:
            raise RuntimeError(
                "PerfDog could not be focused: "
                f"foreground={int(user32.GetForegroundWindow())}, target={hwnd}"
            )

        window = self._find_window()
        self._assert_geometry(window)
        payload = self._window_payload(window)
        LOGGER.info("PerfDog focused without resizing: %s", payload)
        return payload

    @staticmethod
    def assert_foreground(expected_hwnd: int) -> None:
        foreground = int(ctypes.windll.user32.GetForegroundWindow())
        if foreground != int(expected_hwnd):
            raise RuntimeError(
                "PerfDog lost foreground focus: "
                f"foreground={foreground}, expected={expected_hwnd}"
            )

    def click(self, point: Point) -> None:
        self.mouse.click(point.x, point.y)

    def click_boundary(self, point: Point, hwnd: int) -> None:
        """Stop recording at the deadline; fail on a window mismatch without waiting for focus."""
        self.assert_foreground(hwnd)
        window = self._find_window()
        self._assert_geometry(window)
        if int(window._hWnd) != hwnd:
            raise RuntimeError("PerfDog window changed at the capture boundary")
        self.mouse.click(point.x, point.y, _pause=False)
