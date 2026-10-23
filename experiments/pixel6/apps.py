"""Pixel 6 flows for Douyin, Kuaishou, iQIYI, Taobao and Weibo."""
from __future__ import annotations

import logging
import time

import uiautomator2 as u2

from app_checks import validate_pixel_config

logger = logging.getLogger("pavo_app_flow")
PACKAGES = {
    "dy": "com.ss.android.ugc.aweme", "ks": "com.smile.gifmaker",
    "aqy": "com.qiyi.video", "tb": "com.taobao.taobao", "wb": "com.sina.weibo",
}


class AppTester:
    def __init__(self, serial=None, app_config=None):
        if not serial or not app_config:
            raise ValueError("An explicit serial and App configuration are required")
        self.serial, self.app_config = serial, app_config
        self.stop_event = None
        self.finish_current_scenario_on_stop = False
        self.current_config = None
        self.d = u2.connect(serial)

    def set_stop_event(self, event, *, finish_current_scenario=False):
        self.stop_event = event
        self.finish_current_scenario_on_stop = bool(event is not None and finish_current_scenario)

    def set_repeated_scenario_mode(self, enabled):
        # A full round includes return to the in-App entry, without exit or restart.
        self.repeat_scenario_rounds = bool(enabled)

    def _stop_requested(self):
        if getattr(self, "finish_current_scenario_on_stop", False):
            return False
        return self.stop_event is not None and self.stop_event.is_set()

    def _wait_or_stop(self, seconds):
        if getattr(self, "finish_current_scenario_on_stop", False):
            time.sleep(seconds)
            return False
        if self.stop_event is not None:
            return self.stop_event.wait(seconds)
        time.sleep(seconds)
        return False

    def _get_package_name(self, app_name):
        return PACKAGES[app_name]

    def _click_target(self, name):
        target = self.d(**self.current_config["targets"][name])
        if not target.wait(timeout=10):
            raise RuntimeError("Configured target was not found: " + name)
        target.click()

    def start_app(self, app_name):
        package = self._get_package_name(app_name)
        validate_pixel_config(self.app_config, app_name, package)
        self.d.app_stop(package)
        time.sleep(2)
        started = time.monotonic()
        self.d.app_start(package)
        if not self.d.app_wait(package, front=True, timeout=30):
            raise RuntimeError("Configured App did not become foreground")
        time.sleep(3)
        return True, time.monotonic() - started

    def close_app(self, app_name):
        package = self._get_package_name(app_name)
        self.d.app_stop(package)
        if (self.d.app_current() or {}).get("package") == package:
            raise RuntimeError("App remained foreground after stop")
        return True

    def run_test(self, app_name, swipe_times=6):
        if app_name not in PACKAGES or type(swipe_times) is not int or swipe_times <= 0:
            raise ValueError("Unsupported App or invalid work count")
        package = PACKAGES[app_name]
        config = validate_pixel_config(self.app_config, app_name, package)
        self.current_config = config
        if list(self.d.window_size()) != config["screen_size"]:
            raise RuntimeError("Display differs from the App coordinate layout")
        methods = {"dy": self._test_douyin, "ks": self._test_kuaishou,
                   "tb": self._test_taobao, "wb": self._test_weibo,
                   "aqy": self._test_aiqiyi}
        # Raise on click/swipe failure rather than recording success.
        methods[app_name](swipe_times)
        if (not self._stop_requested() and app_name != "aqy"
                and getattr(self, "repeat_scenario_rounds", False)):
            for step in config.get("return_steps", []):
                self.d.press(step["key"])
                time.sleep(step.get("wait_s", 1))
        if not self._stop_requested() and (self.d.app_current() or {}).get("package") != package:
            raise RuntimeError("Configured App left the foreground")
        return True

    def _test_aiqiyi(self, playback_seconds):
        title_text = self.current_config["playback"]["fixed_video_title"]

        def require_player(timeout=10):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                current = self.d.app_current() or {}
                if (current.get("package") == PACKAGES["aqy"]
                        and str(current.get("activity", "")).endswith("PlayerActivity")):
                    return
                time.sleep(0.5)
            raise RuntimeError("iQIYI did not enter PlayerActivity")

        time.sleep(3)
        self.d.click(964, 2221)
        time.sleep(3)
        entry = self.d(textStartsWith="观看历史")
        if not entry.wait(timeout=10):
            raise RuntimeError("Offline video entry was not found")
        entry.click()
        time.sleep(3)
        title = self.d(text=title_text)
        if not title.wait(timeout=10):
            raise RuntimeError("Configured offline video was not found")
        title.click()
        require_player()
        self.d.click(1025, 674)
        time.sleep(2)
        require_player(timeout=3)
        self._wait_or_stop(playback_seconds)

    def _test_douyin(self, swipe_times=12):
        """Douyin flow"""
        logger.info("=== Run Douyin flow ===")

        time.sleep(5)
        logger.info("Action: tap the Me tab")
        try:
            self.d.click(972, 2210)
        except Exception as e:
            logger.error(f"Failed to tap the Me tab: {str(e)}")
            raise

        time.sleep(6)

        logger.info("Action: tap Following")
        try:
            self.d.click(392,707)
        except Exception as e:
            logger.error(f"Failed to tap Following: {str(e)}")
            raise

        time.sleep(6)

        logger.info("Action: select the configured target account")
        try:
            self._click_target("target_account")
        except Exception as e:
            logger.error(f"Failed to select the target account: {str(e)}")
            raise

        time.sleep(6)

        logger.info("Action: open the configured video")
        try:
            time.sleep(3)
            self._click_target("target_video")
            logger.info("View element tapped")
        except Exception as e:
            logger.error(f"Failed to open the configured video: {str(e)}")
            raise

        time.sleep(7)

        logger.info(f"Browse videos with {swipe_times} planned swipes...")
        swipe_count = 0
        self.last_douyin_swipe_target = swipe_times
        self.last_douyin_swipe_count = 0

        for i in range(swipe_times):
            if self._stop_requested():
                logger.info("Window ended; no further Douyin swipe")
                break

            logger.info(f"Action: swipe up - {i+1}/{swipe_times}")
            try:
                self.d.swipe(540, 1800, 540, 500, duration=0.5)
                swipe_count += 1
                self.last_douyin_swipe_count = swipe_count
                logger.info(f"Swipe completed ({swipe_count}/{swipe_times})")
            except Exception as e:
                logger.error(f"Swipe failed: {str(e)}")
                raise
            
            logger.info(f"Wait 4 seconds for video playback...")
            if self._wait_or_stop(4):
                logger.info("Window ended; finish the video-playback wait early")
                break
        
        logger.info(f"Swipe flow complete: {swipe_count}/{swipe_times} swipes")
        logger.info(f"Douyin flow complete")

    def _test_taobao(self, swipe_times=11):
        """Taobao flow; swipe_times is the swipe count"""
        logger.info("=== Run Taobao flow ===")

        time.sleep(3)

        logger.info("Action: tap the search box")
        search_bar = self.d(description="搜索栏")
        if not search_bar.wait(timeout=5):
            raise RuntimeError("Search bar not found on the Taobao entry page")
        search_bar.click()
        logger.info("Entered search through the search-bar node")

        time.sleep(3)

        query = self.current_config["search_query"]
        logger.info("Action: select the configured product search history entry")
        history_item = self.d(
            text=query, className="android.widget.TextView"
        )
        if not history_item.wait(timeout=5):
            raise RuntimeError("Configured search-history entry not found on the Taobao search page")
        history_item.click()

        result_toolbar = self.d(resourceId="com.taobao.taobao:id/search_toolbar")
        result_keyword = self.d(
            resourceId="com.taobao.taobao:id/searchEdit", text=query
        )
        if not result_toolbar.wait(timeout=10) or not result_keyword.exists:
            raise RuntimeError("History selection did not open the configured product results")
        logger.info("Configured product-result page verified")
        time.sleep(2)

        logger.info("Action: sort by sales")
        self.d.click(230, 304)

        time.sleep(3)
        if not result_toolbar.exists or not result_keyword.exists:
            raise RuntimeError("Left the expected result page after sorting by sales")
        logger.info("Sales-sorted result page verified")

        logger.info(f"Browse products with {swipe_times} planned swipes...")
        swipe_count = 0

        for i in range(swipe_times):
            if self._stop_requested():
                logger.info("Window ended; no further Taobao swipe")
                break
            logger.info(f"Action: swipe up - {i+1}/{swipe_times}")
            try:
                self.d.swipe(540, 1800, 540, 500, duration=0.5)
                swipe_count += 1
                logger.info(f"Swipe completed ({swipe_count}/{swipe_times})")
            except Exception as e:
                raise RuntimeError(f"Taobao swipe {i + 1} failed: {e}") from e

            logger.info(f"Wait 3 seconds for products to load...")
            if self._wait_or_stop(3):
                logger.info("Window ended; finish the product-loading wait early")
                break

        logger.info(f"Swipe flow complete: {swipe_count}/{swipe_times} swipes")


    def _test_weibo(self, swipe_times=7):
        """Weibo flow: enter the configured profile once; swipe_times controls the swipe count."""
        logger.info("=== Run Weibo flow ===")

        time.sleep(2)

        logger.info("Action: tap Discover")
        try:
            self.d.click(538, 2206)
            logger.info("Discover tapped")
        except Exception as e:
            logger.error(f"Failed to tap Discover: {str(e)}")
            raise

        time.sleep(2)
        logger.info("Action: tap the search box")
        try:
            result = self.d.shell("input tap 426 183")
            if getattr(result, "exit_code", 0) not in (None, 0):
                raise RuntimeError(f"Device tap command failed: {result}")
            logger.info("Search box tapped")
        except Exception as e:
            logger.error(f"Failed to tap the search box: {str(e)}")
            raise

        time.sleep(2)

        logger.info("Action: search for the configured target account")
        try:
            self._click_target("search_history")
            logger.info("Configured target-account search completed")
        except Exception as e:
            logger.error(f"Configured target-account search failed: {str(e)}")
            raise

        time.sleep(2)

        logger.info("Action: open the target account's posts")
        try:
            profile = self.d(**self.current_config["targets"]["target_profile"])
            if not profile.wait(timeout=10):
                raise RuntimeError("Configured profile card was not found")
            avatar = profile.sibling(className="android.widget.FrameLayout")
            if not avatar.exists:
                raise RuntimeError("Configured profile card has no avatar entry")
            x, y = avatar.center()
            result = self.d.shell(f"input tap {x} {y}")
            if getattr(result, "exit_code", 0) not in (None, 0):
                raise RuntimeError(f"Device profile tap failed: {result}")
            logger.info("Target account's posts opened")
        except Exception as e:
            logger.error(f"Failed to open the target account's posts: {str(e)}")
            raise

        time.sleep(2)

        if not (self.d.app_current() or {}).get("activity", "").endswith("ProfileInfoActivity"):
            raise RuntimeError("Weibo did not enter the configured account's profile")
        logger.info(f"Browse Weibo with {swipe_times} swipes at 3-second intervals")
        for i in range(swipe_times):
            if self._stop_requested():
                logger.info("Window ended; no further Weibo swipe")
                break
            logger.info(f"Action: swipe Weibo - {i+1}/{swipe_times}")
            try:
                result = self.d.shell("input swipe 540 1800 540 500 600")
                if getattr(result, "exit_code", 0) not in (None, 0):
                    raise RuntimeError(f"Device swipe command failed: {result}")
                logger.info(f"Swipe completed ({i+1}/{swipe_times})")
            except Exception as e:
                logger.error(f"Swipe failed: {str(e)}")
                raise

            if self._wait_or_stop(3):
                logger.info("Window ended; finish the Weibo browsing wait early")
                break

        logger.info("Weibo swipes complete")

        logger.info("Search flow complete")


    def _test_kuaishou(self, swipe_times=12):
        """Kuaishou configured-profile video feed; swipe_times controls the swipe count."""
        logger.info("=== Run Kuaishou flow ===")

        time.sleep(1)

        logger.info("Action: tap the Me tab")
        try:
            self.d.click(973, 2224)  # Me tab
            logger.info("Me tab tapped")
        except Exception as e:
            logger.error(f"Failed to tap the Me tab: {str(e)}")
            raise

        time.sleep(2)

        logger.info("Action: tap Following")
        try:
            self.d.click(674, 497)  # Following
            logger.info("Following tapped")
        except Exception as e:
            logger.error(f"Failed to tap Following: {str(e)}")
            raise

        time.sleep(2)

        logger.info("Action: select the target account")
        try:
            self._click_target("target_account")
            logger.info("Target account selected")
        except Exception as e:
            logger.error(f"Failed to select the target account: {str(e)}")
            raise

        time.sleep(2)

        logger.info("Action: open the configured video")
        try:
            self._click_target("target_video")
            logger.info("Video opened")
        except Exception as e:
            logger.error(f"Failed to open the video: {str(e)}")
            raise

        time.sleep(2)

        logger.info(f"Browse videos with {swipe_times} planned swipes...")
        swipe_count = 0

        for i in range(swipe_times):
            if self._stop_requested():
                logger.info("Window ended; no further Kuaishou swipe")
                break

            logger.info(f"Action: swipe up - {i+1}/{swipe_times}")
            try:
                result = self.d.shell("input swipe 540 1800 540 500 600")
                if getattr(result, "exit_code", 0) not in (None, 0):
                    raise RuntimeError(f"Device swipe command failed: {result}")
                swipe_count += 1
                logger.info(f"Swipe completed ({swipe_count}/{swipe_times})")
            except Exception as e:
                logger.error(f"Swipe failed: {str(e)}")
                raise

            logger.info("Wait 2.5 seconds for video playback...")
            if self._wait_or_stop(2.5):
                logger.info("Window ended; finish the Kuaishou playback wait early")
                break

        logger.info(f"Swipe flow complete: {swipe_count}/{swipe_times} swipes")
        logger.info(f"Kuaishou flow complete")

if __name__ == "__main__":
    raise SystemExit("Use experiments/run_main.py; this module never selects a device implicitly.")
