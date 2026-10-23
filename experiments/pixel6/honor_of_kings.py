"""Honor of Kings training-camp interaction and Pixel capture adapter."""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from io import BytesIO
from pathlib import Path

import uiautomator2 as u2
from PIL import Image, ImageFilter

from device_control import DeviceDriver
from app_runner import FlowStopped

logger = logging.getLogger("pavo.game")

GAME_PACKAGE = "com.tencent.tmgp.sgame"
GAME_ACTIVITY = "com.tencent.tmgp.sgame.SGameActivity"
GAME_EXPECTED_SIZE = (2400, 1080)
GAME_SKILL_WINDOW_S = 12.0
GAME_SKILL_INTERVAL_S = 2.0
GAME_MOVE_DURATION_MS = 7000
GAME_LOBBY_WAIT_S = 15.0
GAME_LOBBY_READY_TIMEOUT_S = 90.0
GAME_LOBBY_READY_POLL_S = 1.5
GAME_LOBBY_READY_CONSECUTIVE = 2
GAME_LOBBY_GOLDEN_RATIO_MIN = 0.15
GAME_LOBBY_BLUE_RATIO_MIN = 0.04
GAME_LOBBY_PROFILE_EDGE_RATIO_MIN = 0.07
GAME_LOBBY_CARDS_EDGE_RATIO_MIN = 0.065
GAME_LOBBY_EDGE_LUMA_MIN = 45
GAME_ENTRY_READY_TIMEOUT_S = 90.0
GAME_ENTRY_READY_POLL_S = 0.5
GAME_START_GOLDEN_RATIO_MIN = 0.20
GAME_START_BLUE_RATIO_MAX = 0.02
GAME_START_EXIT_BLUE_RATIO_MIN = 0.20
GAME_START_BUTTON_WARM_RATIO_MIN = 0.20
GAME_START_BUTTON_RED_RATIO_MAX = 0.55
GAME_START_BUTTON_DARK_RATIO_MAX = 0.20
GAME_START_BUTTON_BLUE_RATIO_MAX = 0.20
GAME_START_RATING_WHITE_RATIO_MIN = 0.15
GAME_START_MAX_ATTEMPTS = 3
GAME_TEAM_BINDING_PANEL_BLUE_RATIO_MIN = 0.60
GAME_TEAM_BINDING_CANCEL_BLUE_RATIO_MIN = 0.60
GAME_TEAM_BINDING_CONFIRM_WARM_RATIO_MIN = 0.35
# A slow scheduler can keep the Start page visible while the transition is
# already queued. Retrying too early can land the second tap on the lobby.
GAME_START_RETRY_WAIT_S = 15.0
GAME_LOBBY_POPUP_DARK_RATIO_MIN = 0.75
GAME_LOBBY_POPUP_X_WHITE_RATIO_MIN = 0.30
GAME_LOBBY_POPUP_X_CORE_WHITE_RATIO_MIN = 0.80
GAME_LOBBY_LIVE_POPUP_DARK_RATIO_MIN = 0.55
GAME_LOBBY_LIVE_POPUP_X_WHITE_RATIO_MIN = 0.15
GAME_LOBBY_LIVE_POPUP_X_CORE_WHITE_RATIO_MIN = 0.20
GAME_LOBBY_KPL_POPUP_DARK_RATIO_MIN = 0.45
GAME_LOBBY_KPL_POPUP_X_WHITE_RATIO_MIN = 0.10
GAME_LOBBY_KPL_POPUP_X_CORE_WHITE_RATIO_MIN = 0.80
GAME_LOBBY_POPUP_MAX_DISMISSALS = 3
GAME_LOBBY_POPUP_WAIT_S = 1.0
GAME_TRAINING_PANEL_CYAN_RATIO_MIN = 0.10
GAME_TRAINING_SCORE_BLUE_RATIO_MAX = 0.07
GAME_TRAINING_OPTION_COUNT = 5
GAME_SKILL_LEARN_ATTEMPTS = 1
GAME_SKILL_LEARN_INTERVAL_S = 0.0
GAME_SKILL_LEARN_SETTLE_S = 1.0
GAME_SKILL_PLUS_YELLOW_MAX = 0.02
GAME_SKILL_ICON_ORANGE_MIN = 0.04

GAME_NAVIGATION_WAIT_S = 2.0
GAME_DIALOG_WAIT_S = 1.0
GAME_SELECTION_WAIT_S = 1.5
GAME_OPPONENT_SELECTION_WAIT_S = 3.0
GAME_START_BATTLE_RETRY_WAIT_S = 3.0
GAME_START_BATTLE_ATTEMPTS = 2
GAME_BATTLE_LOAD_WAIT_S = 50.0
GAME_TRAINING_OPTION_WAIT_S = 1.0
GAME_POST_MOVE_WAIT_S = 1.0

REFERENCE_POINTS = {
    "start_game": (1200, 845),
    "dismiss_lobby_popup": (2018, 150),
    "dismiss_lobby_live_popup": (1868, 104),
    "dismiss_lobby_kpl_popup": (2104, 116),
    "battle": (996, 815),
    "improve_skills": (1890, 60),
    "basic_training": (1500, 560),
    "dismiss_training_tutorial": (1200, 995),
    "select_angela": (390, 410),
    "choose_opponent": (2140, 1000),
    "select_bian_que": (190, 820),
    "start_battle": (2140, 1000),
    "cancel_team_binding": (1000, 960),
    "self_invincible": (433, 72),
    "opponent_invincible": (433, 200),
    "no_skill_cooldown": (351, 72),
    "reset_level": (229, 72),
    "level_up_once": (143, 72),
    "learn_skill_1": (1624, 832),
    "learn_skill_2": (1740, 636),
    "skill_1": (1745, 964),
    "skill_2": (1872, 735),
    "settings": (2242, 47),
    "exit_match": (1200, 957),
    "continue_after_exit": (1200, 980),
    "move_start": (472, 836),
    "move_end": (608, 698),
}

REFERENCE_REGIONS = {
    "lobby_battle_region": (780, 730, 1210, 900),
    "lobby_profile_region": (80, 0, 700, 160),
    "lobby_cards_region": (1850, 100, 2320, 750),
    "start_exit_region": (2100, 0, 2300, 110),
    "start_button_region": (1000, 780, 1400, 880),
    "start_rating_region": (1990, 835, 2130, 1015),
    "team_binding_panel_region": (500, 100, 1900, 880),
    "team_binding_cancel_region": (820, 900, 1170, 1030),
    "team_binding_confirm_region": (1230, 900, 1580, 1030),
    "lobby_popup_dim_region": (0, 0, 600, 250),
    "lobby_popup_x_region": (1995, 123, 2050, 178),
    "lobby_popup_x_core_region": (2010, 142, 2026, 158),
    "lobby_live_popup_x_region": (1835, 75, 1900, 135),
    "lobby_live_popup_x_core_region": (1860, 96, 1876, 112),
    "lobby_kpl_popup_x_region": (2075, 80, 2140, 150),
    "lobby_kpl_popup_x_core_region": (2096, 108, 2112, 124),
    "training_panel_region": (100, 0, 1000, 400),
    "training_score_region": (1600, 0, 1950, 160),
    "skill_1_plus_region": (1585, 770, 1675, 865),
    "skill_2_plus_region": (1695, 575, 1795, 680),
    "skill_1_icon_region": (1670, 885, 1815, 1040),
    "skill_2_icon_region": (1790, 660, 1950, 825),
}


def load_game_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if (config.get("schema_version") != 1 or config.get("platform") != "pixel6"
            or config.get("package") != GAME_PACKAGE
            or config.get("version_name") != "11.4.1.1"):
        raise ValueError("Use the Pixel Honor of Kings v11.4.1.1 configuration")
    if config.get("reference_size") != list(GAME_EXPECTED_SIZE):
        raise ValueError("The game layout requires 2400 x 1080 landscape")
    if not config.get("graphics_settings") or str(config["graphics_settings"]).startswith("SET_"):
        raise ValueError("Record the fixed in-game graphics settings")
    width, height = GAME_EXPECTED_SIZE
    for group, reference, length in (("points", REFERENCE_POINTS, 2),
                                     ("regions", REFERENCE_REGIONS, 4)):
        values = config.get(group, {})
        if set(values) != set(reference):
            raise ValueError("Missing or unknown game layout entries: " + group)
        for name, value in values.items():
            if (not isinstance(value, list) or len(value) != length
                    or any(type(v) is not int for v in value)):
                raise ValueError("Invalid game layout entry: " + name)
            if length == 2:
                valid = 0 <= value[0] < width and 0 <= value[1] < height
            else:
                valid = 0 <= value[0] < value[2] <= width and 0 <= value[1] < value[3] <= height
            if not valid:
                raise ValueError("Game layout entry is outside the reference display: " + name)
    return config


class GameFlow:
    def __init__(self, serial, adb, config, directory):
        self.serial, self.adb_executable = serial, adb
        self.directory = Path(directory)
        self.points, self.regions = config["points"], config["regions"]
        self.d = u2.connect(serial)
        self.stop_event = threading.Event()
        self.scene_ready = threading.Event()
        self.thread = None
        self.error = None
        self.deadline = None
        self.rounds = 0
        self.skill_pairs = 0
        self.cycle_interrupted = False
        self.finished_monotonic_s = None
        self.game_lobby_ready = False
        self.training_started = False
        self.game_in_match = False
        self.game_skill_learning_image = None
        self.game_skill_learning_signature = None
        self.actions = []

    def _wait_or_stop(self, seconds):
        return self.stop_event.wait(seconds)

    def launch(self):
        self.d.app_stop(GAME_PACKAGE)
        time.sleep(2)
        self.d.app_start(GAME_PACKAGE, GAME_ACTIVITY)
        time.sleep(3)
        time.sleep(3)

    def prepare_training(self):
        self._prepare_game()
        self._begin_training()
        self._enter_game_training_scene()
        image = self._fast_screenshot()
        self.training_signature = self._game_training_signature(image)
        if not self.training_signature["ready"]:
            raise RuntimeError("Honor of Kings training scene is not ready")
        self.game_in_match = True
        self.scene_ready.set()
        image.save(self.directory / "training_ready.png")
        self.game_skill_learning_image.save(self.directory / "skills_ready.png")

    def start(self):
        if self.thread is not None or not self.scene_ready.is_set() or self.deadline is None:
            raise RuntimeError("Prepare the training scene and measurement deadline before starting")
        self.thread = threading.Thread(target=self._run, name="honor-of-kings", daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while not self.stop_event.is_set() and time.monotonic() < self.deadline:
                self._skill_sequence(5)
                self.rounds += 1
        except FlowStopped:
            self.cycle_interrupted = True
        except BaseException as exc:
            self.error = exc
        finally:
            self.finished_monotonic_s = time.monotonic()

    def check(self):
        if self.error is not None:
            raise RuntimeError("Honor of Kings interaction failed") from self.error
        if self.thread is not None and not self.thread.is_alive() and self.finished_monotonic_s is None:
            raise RuntimeError("Honor of Kings interaction exited unexpectedly")

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(20)
            if self.thread.is_alive():
                raise RuntimeError("Honor of Kings interaction did not stop")

    def close(self):
        if self.thread is not None and self.thread.is_alive():
            raise RuntimeError("Stop the game interaction before closing the App")
        self.stop_event.clear()
        try:
            if self.game_in_match and self.d.app_current().get("package") == GAME_PACKAGE:
                self._exit_game_match()
        finally:
            self.d.app_stop(GAME_PACKAGE)
            (self.directory / "game_actions.json").write_text(
                json.dumps(self.actions, indent=2), encoding="utf-8")

    def _fast_screenshot(self):
        """Capture the game surface without UIAutomator's slow HTTP path."""
        command = [self.adb_executable]
        if self.serial:
            command.extend(("-s", self.serial))
        command.extend(("exec-out", "screencap", "-p"))
        last_error = None
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    timeout=20,
                )
                return Image.open(BytesIO(result.stdout)).convert("RGB")
            except (subprocess.SubprocessError, OSError) as exc:
                last_error = exc
                logger.warning(
                    "Fast Honor of Kings screenshot attempt %d/3 failed: %s",
                    attempt,
                    exc,
                )
                if attempt < 3 and self._wait_or_stop(1.0):
                    raise RuntimeError(
                        "Honor of Kings screenshot retry received a stop request"
                    )
        raise RuntimeError(
            f"Unable to capture Honor of Kings screenshot after 3 attempts: {last_error}"
        )

    def _wait_for_game_landscape(self, timeout=30.0):
        deadline = time.monotonic() + timeout
        size = self.d.window_size()
        while time.monotonic() < deadline:
            size = self.d.window_size()
            if tuple(size) == GAME_EXPECTED_SIZE:
                logger.info(f"Honor of Kings landscape ready: {size[0]}x{size[1]}")
                return
            time.sleep(0.5)
        raise RuntimeError(
            f"Honor of Kings did not reach expected landscape {GAME_EXPECTED_SIZE}; current window is {size}"
        )

    def _ensure_game_foreground(self, step):
        current = self.d.app_current()
        if current.get("package") != GAME_PACKAGE:
            raise RuntimeError(f"Honor of Kings lost foreground at {step}: {current}")
        logger.info(f"Honor of Kings foreground check passed: {step}")

    def _game_lobby_signature(self, image=None):
        image = (image or self.d.screenshot()).convert("RGB")
        width, height = image.size

        def scaled_crop(region):
            left, top, right, bottom = region
            return image.crop(
                (
                    round(left * width / GAME_EXPECTED_SIZE[0]),
                    round(top * height / GAME_EXPECTED_SIZE[1]),
                    round(right * width / GAME_EXPECTED_SIZE[0]),
                    round(bottom * height / GAME_EXPECTED_SIZE[1]),
                )
            )

        pixels = list(scaled_crop(self.regions["lobby_battle_region"]).getdata())
        if not pixels:
            return {
                "golden_ratio": 0.0,
                "blue_ratio": 0.0,
                "profile_edge_ratio": 0.0,
                "cards_edge_ratio": 0.0,
                "ready": False,
            }
        golden = sum(
            1
            for red, green, blue in pixels
            if red > 130 and green > 95 and blue < 100 and red > blue * 1.35
        )
        blue = sum(
            1
            for red, green, blue in pixels
            if blue > red * 1.2 and blue > green * 1.05 and blue > 80
        )
        golden_ratio = golden / len(pixels)
        blue_ratio = blue / len(pixels)

        def edge_ratio(region):
            edges = scaled_crop(region).convert("L").filter(ImageFilter.FIND_EDGES)
            edge_pixels = list(edges.getdata())
            return sum(
                value >= GAME_LOBBY_EDGE_LUMA_MIN for value in edge_pixels
            ) / len(edge_pixels)

        profile_edge_ratio = edge_ratio(self.regions["lobby_profile_region"])
        cards_edge_ratio = edge_ratio(self.regions["lobby_cards_region"])
        return {
            "golden_ratio": golden_ratio,
            "blue_ratio": blue_ratio,
            "profile_edge_ratio": profile_edge_ratio,
            "cards_edge_ratio": cards_edge_ratio,
            "ready": (
                profile_edge_ratio >= GAME_LOBBY_PROFILE_EDGE_RATIO_MIN
                and cards_edge_ratio >= GAME_LOBBY_CARDS_EDGE_RATIO_MIN
            ),
        }

    def _game_start_signature(self, image=None):
        image = (image or self.d.screenshot()).convert("RGB")
        width, height = image.size
        def scaled_pixels(region):
            left, top, right, bottom = region
            box = (
                round(left * width / GAME_EXPECTED_SIZE[0]),
                round(top * height / GAME_EXPECTED_SIZE[1]),
                round(right * width / GAME_EXPECTED_SIZE[0]),
                round(bottom * height / GAME_EXPECTED_SIZE[1]),
            )
            return list(image.crop(box).getdata())

        exit_pixels = scaled_pixels(self.regions["start_exit_region"])
        button_pixels = scaled_pixels(self.regions["start_button_region"])
        rating_pixels = scaled_pixels(self.regions["start_rating_region"])
        if not exit_pixels or not button_pixels or not rating_pixels:
            return {
                "exit_blue_ratio": 0.0,
                "button_warm_ratio": 0.0,
                "button_red_ratio": 0.0,
                "button_dark_ratio": 0.0,
                "button_blue_ratio": 0.0,
                "rating_white_ratio": 0.0,
                "button_ready": False,
                "ready": False,
            }
        exit_blue_ratio = sum(
            1
            for red, green, blue in exit_pixels
            if blue > red * 1.15 and blue > green * 1.03 and blue > 70
        ) / len(exit_pixels)
        button_warm_ratio = sum(
            1
            for red, green, blue in button_pixels
            if red > 150 and green > 110 and blue < 130 and red > blue * 1.25
        ) / len(button_pixels)
        button_red_ratio = sum(
            1
            for red, green, blue in button_pixels
            if red > 80 and red > green * 1.25 and red > blue * 1.15
        ) / len(button_pixels)
        button_dark_ratio = sum(
            1
            for red, green, blue in button_pixels
            if red < 100 and green < 100 and blue < 100
        ) / len(button_pixels)
        button_blue_ratio = sum(
            1
            for red, green, blue in button_pixels
            if blue > red * 1.2 and blue > green * 1.05 and blue > 80
        ) / len(button_pixels)
        rating_white_ratio = sum(
            1
            for red, green, blue in rating_pixels
            if min(red, green, blue) >= 170
            and max(red, green, blue) - min(red, green, blue) <= 75
        ) / len(rating_pixels)
        button_ready = (
            button_warm_ratio >= GAME_START_BUTTON_WARM_RATIO_MIN
            and button_red_ratio <= GAME_START_BUTTON_RED_RATIO_MAX
            and button_dark_ratio <= GAME_START_BUTTON_DARK_RATIO_MAX
            and button_blue_ratio <= GAME_START_BUTTON_BLUE_RATIO_MAX
        )
        return {
            "exit_blue_ratio": exit_blue_ratio,
            "button_warm_ratio": button_warm_ratio,
            "button_red_ratio": button_red_ratio,
            "button_dark_ratio": button_dark_ratio,
            "button_blue_ratio": button_blue_ratio,
            "rating_white_ratio": rating_white_ratio,
            "button_ready": button_ready,
            # The training-mode menu also has blue content in the top-right
            # region. Require the central Start button so that it cannot be
            # mistaken for the account Start page.
            "ready": (
                exit_blue_ratio >= GAME_START_EXIT_BLUE_RATIO_MIN
                and button_ready
                and rating_white_ratio >= GAME_START_RATING_WHITE_RATIO_MIN
            ),
        }

    def _game_lobby_popup_signature(self, image=None):
        image = (image or self.d.screenshot()).convert("RGB")
        width, height = image.size

        def scaled_pixels(region):
            left, top, right, bottom = region
            box = (
                round(left * width / GAME_EXPECTED_SIZE[0]),
                round(top * height / GAME_EXPECTED_SIZE[1]),
                round(right * width / GAME_EXPECTED_SIZE[0]),
                round(bottom * height / GAME_EXPECTED_SIZE[1]),
            )
            return list(image.crop(box).getdata())

        dim_pixels = scaled_pixels(self.regions["lobby_popup_dim_region"])
        common_x_pixels = scaled_pixels(self.regions["lobby_popup_x_region"])
        common_x_core_pixels = scaled_pixels(self.regions["lobby_popup_x_core_region"])
        live_x_pixels = scaled_pixels(self.regions["lobby_live_popup_x_region"])
        live_x_core_pixels = scaled_pixels(self.regions["lobby_live_popup_x_core_region"])
        kpl_x_pixels = scaled_pixels(self.regions["lobby_kpl_popup_x_region"])
        kpl_x_core_pixels = scaled_pixels(self.regions["lobby_kpl_popup_x_core_region"])
        if not all(
            (
                dim_pixels,
                common_x_pixels,
                common_x_core_pixels,
                live_x_pixels,
                live_x_core_pixels,
                kpl_x_pixels,
                kpl_x_core_pixels,
            )
        ):
            return {
                "dark_ratio": 0.0,
                "common_x_white_ratio": 0.0,
                "common_x_core_white_ratio": 0.0,
                "live_x_white_ratio": 0.0,
                "live_x_core_white_ratio": 0.0,
                "kpl_x_white_ratio": 0.0,
                "kpl_x_core_white_ratio": 0.0,
                "common_ready": False,
                "live_ready": False,
                "kpl_ready": False,
                "ready": False,
            }
        dark_ratio = sum(
            1
            for red, green, blue in dim_pixels
            if red < 45 and green < 45 and blue < 45
        ) / len(dim_pixels)
        common_x_white_ratio = sum(
            1
            for red, green, blue in common_x_pixels
            if min(red, green, blue) >= 180
            and max(red, green, blue) - min(red, green, blue) <= 80
        ) / len(common_x_pixels)
        common_x_core_white_ratio = sum(
            1
            for red, green, blue in common_x_core_pixels
            if min(red, green, blue) >= 180
            and max(red, green, blue) - min(red, green, blue) <= 80
        ) / len(common_x_core_pixels)
        live_x_white_ratio = sum(
            1
            for red, green, blue in live_x_pixels
            if min(red, green, blue) >= 180
            and max(red, green, blue) - min(red, green, blue) <= 80
        ) / len(live_x_pixels)
        live_x_core_white_ratio = sum(
            1
            for red, green, blue in live_x_core_pixels
            if min(red, green, blue) >= 180
            and max(red, green, blue) - min(red, green, blue) <= 80
        ) / len(live_x_core_pixels)
        kpl_x_white_ratio = sum(
            1
            for red, green, blue in kpl_x_pixels
            if min(red, green, blue) >= 180
            and max(red, green, blue) - min(red, green, blue) <= 80
        ) / len(kpl_x_pixels)
        kpl_x_core_white_ratio = sum(
            1
            for red, green, blue in kpl_x_core_pixels
            if min(red, green, blue) >= 180
            and max(red, green, blue) - min(red, green, blue) <= 80
        ) / len(kpl_x_core_pixels)
        common_ready = (
            dark_ratio >= GAME_LOBBY_POPUP_DARK_RATIO_MIN
            and common_x_white_ratio >= GAME_LOBBY_POPUP_X_WHITE_RATIO_MIN
            and common_x_core_white_ratio
            >= GAME_LOBBY_POPUP_X_CORE_WHITE_RATIO_MIN
        )
        live_ready = (
            dark_ratio >= GAME_LOBBY_LIVE_POPUP_DARK_RATIO_MIN
            and live_x_white_ratio >= GAME_LOBBY_LIVE_POPUP_X_WHITE_RATIO_MIN
            and live_x_core_white_ratio
            >= GAME_LOBBY_LIVE_POPUP_X_CORE_WHITE_RATIO_MIN
        )
        kpl_ready = (
            dark_ratio >= GAME_LOBBY_KPL_POPUP_DARK_RATIO_MIN
            and kpl_x_white_ratio >= GAME_LOBBY_KPL_POPUP_X_WHITE_RATIO_MIN
            and kpl_x_core_white_ratio
            >= GAME_LOBBY_KPL_POPUP_X_CORE_WHITE_RATIO_MIN
        )
        return {
            "dark_ratio": dark_ratio,
            "common_x_white_ratio": common_x_white_ratio,
            "common_x_core_white_ratio": common_x_core_white_ratio,
            "live_x_white_ratio": live_x_white_ratio,
            "live_x_core_white_ratio": live_x_core_white_ratio,
            "kpl_x_white_ratio": kpl_x_white_ratio,
            "kpl_x_core_white_ratio": kpl_x_core_white_ratio,
            "common_ready": common_ready,
            "live_ready": live_ready,
            "kpl_ready": kpl_ready,
            "ready": common_ready or live_ready or kpl_ready,
        }

    def _game_team_binding_signature(self, image=None):
        image = (image or self.d.screenshot()).convert("RGB")
        width, height = image.size

        def scaled_pixels(region):
            left, top, right, bottom = region
            box = (
                round(left * width / GAME_EXPECTED_SIZE[0]),
                round(top * height / GAME_EXPECTED_SIZE[1]),
                round(right * width / GAME_EXPECTED_SIZE[0]),
                round(bottom * height / GAME_EXPECTED_SIZE[1]),
            )
            return list(image.crop(box).getdata())

        def blue_ratio(pixels):
            return sum(
                blue > red * 1.15 and blue > green * 1.02 and blue > 75
                for red, green, blue in pixels
            ) / len(pixels)

        panel_blue_ratio = blue_ratio(
            scaled_pixels(self.regions["team_binding_panel_region"])
        )
        cancel_blue_ratio = blue_ratio(
            scaled_pixels(self.regions["team_binding_cancel_region"])
        )
        confirm_pixels = scaled_pixels(self.regions["team_binding_confirm_region"])
        confirm_warm_ratio = sum(
            red > 140 and green > 95 and blue < 150 and red > blue * 1.15
            for red, green, blue in confirm_pixels
        ) / len(confirm_pixels)
        return {
            "panel_blue_ratio": panel_blue_ratio,
            "cancel_blue_ratio": cancel_blue_ratio,
            "confirm_warm_ratio": confirm_warm_ratio,
            "ready": (
                panel_blue_ratio >= GAME_TEAM_BINDING_PANEL_BLUE_RATIO_MIN
                and cancel_blue_ratio >= GAME_TEAM_BINDING_CANCEL_BLUE_RATIO_MIN
                and confirm_warm_ratio >= GAME_TEAM_BINDING_CONFIRM_WARM_RATIO_MIN
            ),
        }

    def _dismiss_game_team_binding_if_present(self):
        signature = self._game_team_binding_signature(self._fast_screenshot())
        if not signature["ready"]:
            logger.info("Honor of Kings team-binding popup absent: %s", signature)
            return False
        logger.info("Honor of Kings team-binding popup detected: %s", signature)
        self._game_click("cancel_team_binding", delay=2.0)
        after = self._game_team_binding_signature(self._fast_screenshot())
        if after["ready"]:
            raise RuntimeError(
                "Honor of Kings team-binding popup remained after cancellation: "
                f"{after}"
            )
        logger.info("Honor of Kings team-binding popup dismissed")
        return True

    def _save_game_debug_screenshot(self, label):
        artifact_dir = self.directory
        path = artifact_dir / f"{label}_{time.strftime('%Y%m%d_%H%M%S')}.png"
        self.d.screenshot(str(path))
        logger.error("Honor of Kings debug screenshot saved: %s", path)
        return path

    def _leave_game_start_page(self):
        for attempt in range(1, GAME_START_MAX_ATTEMPTS + 1):
            logger.info(
                "Honor of Kings Start click attempt %d/%d",
                attempt,
                GAME_START_MAX_ATTEMPTS,
            )
            self._game_click("start_game", delay=0)
            deadline = time.monotonic() + GAME_START_RETRY_WAIT_S
            departed_consecutive = 0
            while time.monotonic() < deadline:
                image = self._fast_screenshot()
                lobby_ready = self._game_lobby_signature(image)["ready"]
                popup_ready = self._game_lobby_popup_signature(image)["ready"]
                start_ready = self._game_start_signature(image)["ready"]
                if start_ready and not lobby_ready and not popup_ready:
                    departed_consecutive = 0
                else:
                    departed_consecutive += 1
                if departed_consecutive >= GAME_LOBBY_READY_CONSECUTIVE:
                    logger.info("Honor of Kings Start page departed after attempt %d", attempt)
                    return
                if self._wait_or_stop(GAME_ENTRY_READY_POLL_S):
                    raise RuntimeError("Honor of Kings Start retry received a stop request")
            logger.warning("Honor of Kings remained on the Start page after attempt %d", attempt)

        path = self._save_game_debug_screenshot("game_start_click_failed")
        raise RuntimeError(
            "Honor of Kings remained on the Start page after "
            f"{GAME_START_MAX_ATTEMPTS} conditional clicks; screenshot={path}"
        )

    def _dismiss_game_lobby_popups(self):
        dismissed = 0
        common_dismissed = 0
        live_dismissed = 0
        kpl_dismissed = 0
        while True:
            signature = self._game_lobby_popup_signature(self._fast_screenshot())
            if (
                signature["common_ready"]
                and common_dismissed < GAME_LOBBY_POPUP_MAX_DISMISSALS
            ):
                logger.info(
                    "Honor of Kings common popup detected: dark_ratio=%.4f "
                    "common_x_white_ratio=%.4f core=%.4f; dismissal=%d/%d",
                    signature["dark_ratio"],
                    signature["common_x_white_ratio"],
                    signature["common_x_core_white_ratio"],
                    common_dismissed + 1,
                    GAME_LOBBY_POPUP_MAX_DISMISSALS,
                )
                self._game_click(
                    "dismiss_lobby_popup", delay=GAME_LOBBY_POPUP_WAIT_S
                )
                common_dismissed += 1
                dismissed += 1
                continue
            if (
                signature["kpl_ready"]
                and kpl_dismissed < GAME_LOBBY_POPUP_MAX_DISMISSALS
            ):
                logger.info(
                    "Honor of Kings KPL popup detected: dark_ratio=%.4f "
                    "kpl_x_white_ratio=%.4f core=%.4f; dismissal=%d/%d",
                    signature["dark_ratio"],
                    signature["kpl_x_white_ratio"],
                    signature["kpl_x_core_white_ratio"],
                    kpl_dismissed + 1,
                    GAME_LOBBY_POPUP_MAX_DISMISSALS,
                )
                self._game_click(
                    "dismiss_lobby_kpl_popup", delay=GAME_LOBBY_POPUP_WAIT_S
                )
                kpl_dismissed += 1
                dismissed += 1
                continue
            if (
                signature["live_ready"]
                and live_dismissed < GAME_LOBBY_POPUP_MAX_DISMISSALS
            ):
                logger.info(
                    "Honor of Kings live popup detected: dark_ratio=%.4f "
                    "live_x_white_ratio=%.4f core=%.4f; dismissal=%d/%d",
                    signature["dark_ratio"],
                    signature["live_x_white_ratio"],
                    signature["live_x_core_white_ratio"],
                    live_dismissed + 1,
                    GAME_LOBBY_POPUP_MAX_DISMISSALS,
                )
                self._game_click(
                    "dismiss_lobby_live_popup", delay=GAME_LOBBY_POPUP_WAIT_S
                )
                live_dismissed += 1
                dismissed += 1
                continue
            break

        if signature["ready"]:
            path = self._save_game_debug_screenshot("game_unhandled_lobby_popup")
            raise RuntimeError(
                "Honor of Kings lobby popup remained after all verified "
                f"dismissal coordinates; signature={signature}; screenshot={path}"
            )
        return dismissed

    def _wait_for_game_lobby(self, timeout=GAME_LOBBY_READY_TIMEOUT_S):
        deadline = time.monotonic() + timeout
        consecutive = 0
        signature = {"golden_ratio": 0.0, "blue_ratio": 0.0, "ready": False}
        while time.monotonic() < deadline:
            self._ensure_game_foreground("lobby_gate")
            image = self._fast_screenshot()
            if self._game_lobby_popup_signature(image)["ready"]:
                self._dismiss_game_lobby_popups()
                consecutive = 0
                continue
            signature = self._game_lobby_signature(image)
            if signature["ready"]:
                consecutive += 1
                if consecutive >= GAME_LOBBY_READY_CONSECUTIVE:
                    logger.info(
                        "Honor of Kings lobby visual gate passed: profile_edge_ratio=%.4f "
                        "cards_edge_ratio=%.4f samples=%d",
                        signature["profile_edge_ratio"],
                        signature["cards_edge_ratio"],
                        consecutive,
                    )
                    return
            else:
                consecutive = 0
            start_signature = self._game_start_signature(image)
            if start_signature["ready"]:
                logger.warning(
                    "Honor of Kings lobby gate still sees the Start page; retrying Start"
                )
                self._leave_game_start_page()
                consecutive = 0
                continue
            logger.info(
                "Honor of Kings lobby visual gate waiting: profile_edge_ratio=%.4f "
                "cards_edge_ratio=%.4f "
                "thresholds=%.4f/%.4f",
                signature["profile_edge_ratio"],
                signature["cards_edge_ratio"],
                GAME_LOBBY_PROFILE_EDGE_RATIO_MIN,
                GAME_LOBBY_CARDS_EDGE_RATIO_MIN,
            )
            if self._wait_or_stop(GAME_LOBBY_READY_POLL_S):
                raise RuntimeError("Honor of Kings lobby gate received a stop request")
        raise RuntimeError(
            "Honor of Kings did not reach the real lobby before timeout: "
            f"signature={signature}"
        )

    def _game_training_signature(self, image=None):
        image = (image or self._fast_screenshot()).convert("RGB")
        width, height = image.size

        def scaled_crop(region):
            left, top, right, bottom = region
            return image.crop(
                (
                    round(left * width / GAME_EXPECTED_SIZE[0]),
                    round(top * height / GAME_EXPECTED_SIZE[1]),
                    round(right * width / GAME_EXPECTED_SIZE[0]),
                    round(bottom * height / GAME_EXPECTED_SIZE[1]),
                )
            )

        panel_pixels = list(scaled_crop(self.regions["training_panel_region"]).getdata())
        score_pixels = list(scaled_crop(self.regions["training_score_region"]).getdata())
        skill_1_plus_pixels = list(scaled_crop(self.regions["skill_1_plus_region"]).getdata())
        skill_2_plus_pixels = list(scaled_crop(self.regions["skill_2_plus_region"]).getdata())
        skill_1_icon_pixels = list(scaled_crop(self.regions["skill_1_icon_region"]).getdata())
        skill_2_icon_pixels = list(scaled_crop(self.regions["skill_2_icon_region"]).getdata())
        if not panel_pixels or not score_pixels:
            return {
                "panel_cyan_ratio": 0.0,
                "score_blue_ratio": 1.0,
                "skill_1_plus_yellow_ratio": 1.0,
                "skill_2_plus_yellow_ratio": 1.0,
                "skill_1_icon_orange_ratio": 0.0,
                "skill_2_icon_orange_ratio": 0.0,
                "skills_configured": False,
                "ready": False,
            }

        panel_cyan_ratio = sum(
            1
            for red, green, blue in panel_pixels
            if blue > 100 and green > 80 and blue > red * 1.25
        ) / len(panel_pixels)
        score_blue_ratio = sum(
            1
            for red, green, blue in score_pixels
            if blue > red * 1.2 and blue > green * 1.05 and blue > 80
        ) / len(score_pixels)
        skill_1_plus_yellow_ratio = sum(
            1
            for red, green, blue in skill_1_plus_pixels
            if red > 160 and green > 120 and blue < 100
        ) / len(skill_1_plus_pixels)
        skill_2_plus_yellow_ratio = sum(
            1
            for red, green, blue in skill_2_plus_pixels
            if red > 160 and green > 120 and blue < 100
        ) / len(skill_2_plus_pixels)
        skill_1_icon_orange_ratio = sum(
            1
            for red, green, blue in skill_1_icon_pixels
            if red > 150 and red > green * 1.25 and blue < 100
        ) / len(skill_1_icon_pixels)
        skill_2_icon_orange_ratio = sum(
            1
            for red, green, blue in skill_2_icon_pixels
            if red > 150 and red > green * 1.25 and blue < 100
        ) / len(skill_2_icon_pixels)
        upgrade_markers_cleared = (
            skill_1_plus_yellow_ratio <= GAME_SKILL_PLUS_YELLOW_MAX
            and skill_2_plus_yellow_ratio <= GAME_SKILL_PLUS_YELLOW_MAX
        )
        skill_icons_present = (
            skill_1_icon_orange_ratio >= GAME_SKILL_ICON_ORANGE_MIN
            and skill_2_icon_orange_ratio >= GAME_SKILL_ICON_ORANGE_MIN
        )
        return {
            "panel_cyan_ratio": panel_cyan_ratio,
            "score_blue_ratio": score_blue_ratio,
            "skill_1_plus_yellow_ratio": skill_1_plus_yellow_ratio,
            "skill_2_plus_yellow_ratio": skill_2_plus_yellow_ratio,
            "skill_1_icon_orange_ratio": skill_1_icon_orange_ratio,
            "skill_2_icon_orange_ratio": skill_2_icon_orange_ratio,
            "upgrade_markers_cleared": upgrade_markers_cleared,
            "skill_icons_present": skill_icons_present,
            "skills_configured": upgrade_markers_cleared,
            "ready": panel_cyan_ratio >= GAME_TRAINING_PANEL_CYAN_RATIO_MIN
            and score_blue_ratio <= GAME_TRAINING_SCORE_BLUE_RATIO_MAX,
        }

    def _wait_for_game_entry(self, timeout=GAME_ENTRY_READY_TIMEOUT_S):
        """Wait until either the Start page or the real lobby is visually stable."""
        deadline = time.monotonic() + timeout
        candidate = None
        consecutive = 0
        signature = {"golden_ratio": 0.0, "blue_ratio": 0.0, "ready": False}
        start_signature = {"ready": False}
        while time.monotonic() < deadline:
            self._ensure_game_foreground("entry_gate")
            image = self._fast_screenshot()
            popup_signature = self._game_lobby_popup_signature(image)
            start_signature = self._game_start_signature(image)
            signature = self._game_lobby_signature(image)
            if popup_signature["ready"]:
                self._dismiss_game_lobby_popups()
                candidate = None
                consecutive = 0
                continue
            # Some lobby themes also overlap the Start-page color regions.
            # A complete lobby signature is therefore the stronger verdict.
            if signature["ready"]:
                state = "lobby"
            elif start_signature["ready"]:
                state = "start"
            else:
                state = None

            if state is not None and state == candidate:
                consecutive += 1
            elif state is not None:
                candidate = state
                consecutive = 1
            else:
                candidate = None
                consecutive = 0

            if consecutive >= GAME_LOBBY_READY_CONSECUTIVE:
                logger.info(
                    "Honor of Kings entry visual gate passed: state=%s profile_edge_ratio=%.4f "
                    "cards_edge_ratio=%.4f exit_blue_ratio=%.4f "
                    "button_warm_ratio=%.4f rating_white_ratio=%.4f samples=%d",
                    state,
                    signature["profile_edge_ratio"],
                    signature["cards_edge_ratio"],
                    start_signature["exit_blue_ratio"],
                    start_signature["button_warm_ratio"],
                    start_signature["rating_white_ratio"],
                    consecutive,
                )
                return state
            if self._wait_or_stop(GAME_ENTRY_READY_POLL_S):
                raise RuntimeError("Honor of Kings entry gate received a stop request")
        raise RuntimeError(
            "Honor of Kings did not reach the Start page or real lobby before "
            f"timeout: lobby_signature={signature}, "
            f"start_signature={start_signature}"
        )

    def _game_click(self, step, delay=1.0):
        x, y = self.points[step]
        logger.info(f"Honor of Kings action: {step} ({x}, {y})")
        self.actions.append({"step": step, "point": [x, y], "monotonic_s": time.monotonic()})
        self.d.click(x, y)
        if delay > 0 and self._wait_or_stop(delay):
            raise FlowStopped(f"Honor of Kings flow stopped after {step}")

    def _game_click_redundant(
        self,
        step,
        interval=1.5,
        attempts=5,
        settle_delay=1.0,
    ):
        """Repeat a safe one-shot control with fixed device-side timing."""
        x, y = self.points[step]
        attempts = max(1, int(attempts))
        for attempt in range(1, attempts + 1):
            logger.info(
                "Honor of Kings redundant action: %s (%d, %d), attempt %d/%d",
                step,
                x,
                y,
                attempt,
                attempts,
            )
            # Use the same UIAutomator click path as the training controls above.
            # Under stress, rapid shell-spawned input commands can return before
            # the game has consumed the touch event.
            self.actions.append({"step": step, "point": [x, y], "monotonic_s": time.monotonic()})
            self.d.click(x, y)
            wait_s = interval if attempt < attempts else settle_delay
            if wait_s > 0 and self._wait_or_stop(wait_s):
                raise RuntimeError(
                    f"Honor of Kings flow stopped after {step} attempt {attempt}"
                )

    def _prepare_game(self):
        """Enter the lobby and stop immediately before the Battle-button boundary."""
        logger.info("=== Prepare Honor of Kings lobby ===")
        self._wait_for_game_landscape()
        self._ensure_game_foreground("prepare_start")

        entry_state = self._wait_for_game_entry()
        if entry_state == "start":
            self._leave_game_start_page()
            if self._wait_or_stop(GAME_LOBBY_WAIT_S):
                raise RuntimeError("Honor of Kings lobby wait received a stop request")
        self._wait_for_game_lobby()
        self._ensure_game_foreground("lobby")
        self.game_lobby_ready = True
        self.training_started = False
        logger.info("Honor of Kings lobby ready for the Battle boundary")

    def _begin_training(self):
        """Click the lobby Battle button to prepare the training scene."""
        if not self.game_lobby_ready:
            raise RuntimeError("Honor of Kings lobby is not ready")
        if self.training_started:
            raise RuntimeError("Honor of Kings training entry already started")
        self._ensure_game_foreground("training_entry")
        self._game_click("battle", delay=0)
        self.training_started = True
        logger.info("Honor of Kings Battle clicked")

    def _enter_game_training_scene(self):
        """Navigate to the training camp and apply the fixed scene settings."""
        if not self.training_started:
            raise RuntimeError("Click Battle before preparing the training scene")

        if self._wait_or_stop(GAME_NAVIGATION_WAIT_S):
            raise RuntimeError("Honor of Kings stopped after the Battle boundary")
        self._game_click("improve_skills", delay=GAME_NAVIGATION_WAIT_S)
        self._game_click("basic_training", delay=GAME_NAVIGATION_WAIT_S)
        self._game_click("dismiss_training_tutorial", delay=GAME_DIALOG_WAIT_S)
        self._game_click("select_angela", delay=GAME_SELECTION_WAIT_S)
        self._game_click("choose_opponent", delay=GAME_SELECTION_WAIT_S)
        self._game_click(
            "select_bian_que", delay=GAME_OPPONENT_SELECTION_WAIT_S
        )
        for attempt in range(1, GAME_START_BATTLE_ATTEMPTS):
            self._game_click(
                "start_battle", delay=GAME_START_BATTLE_RETRY_WAIT_S
            )
            logger.info(
                "Honor of Kings retrying Start Battle %d/%d to tolerate a delayed UI",
                attempt,
                GAME_START_BATTLE_ATTEMPTS - 1,
            )
        self._game_click("start_battle", delay=GAME_BATTLE_LOAD_WAIT_S)
        logger.info(
            "Honor of Kings fixed %.0f-second battle-load wait completed",
            GAME_BATTLE_LOAD_WAIT_S,
        )
        self._dismiss_game_team_binding_if_present()

        for step in (
            "self_invincible",
            "opponent_invincible",
            "no_skill_cooldown",
            "reset_level",
            "level_up_once",
        ):
            self._game_click(step, delay=GAME_TRAINING_OPTION_WAIT_S)
        # Each upgrade marker is a one-shot control: one click learns the
        # corresponding skill and removes the marker.
        for step in ("learn_skill_1", "learn_skill_2"):
            self._game_click_redundant(
                step,
                interval=GAME_SKILL_LEARN_INTERVAL_S,
                attempts=GAME_SKILL_LEARN_ATTEMPTS,
                settle_delay=GAME_SKILL_LEARN_SETTLE_S,
            )

        # Validate the one-shot learning actions before movement/combat can
        # grant another level and legitimately show upgrade markers again.
        self.game_skill_learning_image = self._fast_screenshot()
        self.game_skill_learning_signature = self._game_training_signature(
            self.game_skill_learning_image
        )
        if not (
            self.game_skill_learning_signature.get("ready")
            and self.game_skill_learning_signature.get("upgrade_markers_cleared")
        ):
            raise RuntimeError(
                "Honor of Kings skill-learning check failed immediately after "
                f"the two upgrade clicks: {self.game_skill_learning_signature}"
            )

        command = (
            f"input swipe {self.points['move_start'][0]} {self.points['move_start'][1]} "
            f"{self.points['move_end'][0]} {self.points['move_end'][1]} {GAME_MOVE_DURATION_MS}"
        )
        self.actions.append({"step": "move", "duration_ms": GAME_MOVE_DURATION_MS,
                             "monotonic_s": time.monotonic()})
        result = self.d.shell(command)
        if getattr(result, "exit_code", 0) not in (None, 0):
            raise RuntimeError(f"Honor of Kings fixed movement failed: {result}")
        if self._wait_or_stop(GAME_POST_MOVE_WAIT_S):
            raise RuntimeError("Honor of Kings preparation received a stop request")
        self._ensure_game_foreground("training_sequence_complete")
        logger.info(
            "Honor of Kings training setup completed; the scene will be checked "
            "before recording"
        )

    def _skill_sequence(self, skill_rounds=5):
        """Run five closely spaced skill combinations in a fixed short window."""
        rounds = int(skill_rounds)
        if rounds < 1:
            raise ValueError("Honor of Kings skill rounds must be positive")
        if not self.training_started:
            raise RuntimeError("Prepare the training scene before running skill cycles")
        if (rounds - 1) * GAME_SKILL_INTERVAL_S >= GAME_SKILL_WINDOW_S:
            raise ValueError("Honor of Kings skill schedule exceeds skill window")

        logger.info("Honor of Kings skill cycle: %d pairs in %.0fs", rounds, GAME_SKILL_WINDOW_S)
        started = time.monotonic()

        for index in range(rounds):
            target = started + index * GAME_SKILL_INTERVAL_S
            wait_s = target - time.monotonic()
            if wait_s > 0 and self._wait_or_stop(wait_s):
                raise FlowStopped("Honor of Kings measurement reached the recording boundary")
            self._ensure_game_foreground(f"skill_round_{index + 1}")
            self._game_click("skill_1", delay=0.3)
            self._game_click("skill_2", delay=0.3)
            self.skill_pairs += 1
            logger.info(f"Honor of Kings skill combination completed: {index + 1}/{rounds}")

        remaining = started + GAME_SKILL_WINDOW_S - time.monotonic()
        if remaining > 0 and self._wait_or_stop(remaining):
            raise FlowStopped("Honor of Kings measurement reached the recording boundary")
        self._ensure_game_foreground("measurement_complete")
        logger.info(f"Honor of Kings cycle completed: {rounds} skill combinations")

    def _exit_game_match(self):
        logger.info("Exit Honor of Kings training match")
        self._ensure_game_foreground("exit_match")
        self._game_click("settings")
        self._game_click("exit_match", delay=15.0)
        self._game_click("continue_after_exit", delay=3.0)
        self.game_lobby_ready = False
        self.training_started = False
        self.game_in_match = False
        logger.info("Honor of Kings training match exited")

class GameDriver(DeviceDriver):
    def __init__(self, **kwargs):
        super().__init__(**kwargs, app_info=("Honor of Kings", GAME_PACKAGE, "王者荣耀"))

    def validate_app_inputs(self):
        self.game_config = load_game_config(self.config["apps"]["honor_of_kings"]["ui_config"])

    def prepare_app(self):
        self.flow = GameFlow(self.adb.serial, self.adb_executable, self.game_config, self.directory)
        time.sleep(3)
        self.flow.launch()
        self.flow.prepare_training()
        if not self.foreground():
            raise RuntimeError("Honor of Kings left the foreground during preparation")
        self.perfdog.prepare()
        self.perfdog.select_app(initialization_wait_s=4)
        self.prepared = True

    def readiness_scene_active(self):
        return bool(self.flow and self.flow.scene_ready.is_set() and self.flow.game_in_match)

    def prepare_ztt_scene(self):
        # The training camp is already rendered; no skill cycles run during readiness.
        if self.perfdog.started or self.background_prepared:
            raise RuntimeError("zTT readiness must precede game recording")
        try:
            self.ready("before_background")
            self.start_background_load()
            self.ready("after_background")
            self.ztt.start()
            self.background_prepared = True
        finally:
            (self.directory / "readiness.json").write_text(
                json.dumps(self.readiness, indent=2), encoding="utf-8")

    def start_interaction(self):
        started = super().start_interaction()
        if self.ztt:
            self.flow.start()
        return started

    def export(self):
        # Keep the raw export even when the final scene check fails.
        result = super().export()
        image = self.flow._fast_screenshot()
        image.save(self.directory / "training_complete.png")
        final_scene = self.flow._game_training_signature(image)
        record = {
            "initial_scene": self.flow.training_signature,
            "skills_ready": self.flow.game_skill_learning_signature,
            "final_scene": final_scene,
            "skill_cycles": self.flow.rounds,
            "skill_pairs": self.flow.skill_pairs,
            "cycle_interrupted": self.flow.cycle_interrupted,
            "host_interaction_duration_s": result["host_interaction_duration_s"],
        }
        (self.directory / "game_scene.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        if not final_scene["ready"] or self.flow.rounds == 0:
            raise RuntimeError("Honor of Kings did not complete the training-camp capture")
        partial_pairs = self.flow.skill_pairs - 5 * self.flow.rounds
        if not 0 <= partial_pairs <= 5:
            raise RuntimeError("Honor of Kings skill-cycle counters are inconsistent")
        result.update(
            game_skill_cycles=self.flow.rounds, game_skill_pairs=self.flow.skill_pairs,
            game_partial_cycle_pairs=partial_pairs,
            game_cycle_interrupted=self.flow.cycle_interrupted,
            training_setup_in_recording=False,
        )
        if self.ztt:
            result["scene_continues_from_readiness"] = True
            result["skill_interaction_starts_after_readiness"] = True
        return result
