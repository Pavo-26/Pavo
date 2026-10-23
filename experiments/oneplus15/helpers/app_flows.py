"""OnePlus Kuaishou, Taobao and iQIYI flows; other routines are in ../apps.py."""
from __future__ import annotations

import copy
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from .. import apps as automation_module

KUAISHOU_HOME_ACTIVITY = "com.yxcorp.gifshow.HomeActivity"
KUAISHOU_VIDEO_ACTIVITY = "com.yxcorp.gifshow.detail.PhotoDetailActivity"
DOWNLOAD_CENTER_ACTIVITY = "org.qiyi.android.video.ui.phone.download.PhoneDownloadCenterActivity"


def dismiss_kuaishou_splash(automation: Any) -> None:
    device, recorder = automation._require_ready()
    update = device(resourceId="com.smile.gifmaker:id/tv_title", text="版本更新")
    if update.exists:
        close = device(resourceId="com.smile.gifmaker:id/iv_close")
        if not close.exists:
            raise automation_module.AutomationError("Kuaishou update dialog has no close control")
        close.click()
        deadline = time.monotonic() + 5.0
        while update.exists and time.monotonic() < deadline:
            time.sleep(0.25)
        if update.exists:
            raise automation_module.AutomationError("Kuaishou update dialog did not close")
        recorder.event("kuaishou_update_prompt_closed")
    splash = device(resourceId="com.smile.gifmaker:id/splash_content")
    if not splash.exists:
        recorder.event("kuaishou_splash_absent_before_measurement")
        return
    automation.click_first(
        step="kuaishou_splash_skip",
        selectors=[
            {"type": "resource_id", "value": "com.smile.gifmaker:id/skip_text_hot_space"},
            {"type": "resource_id", "value": "com.smile.gifmaker:id/splash_skip_text"},
        ],
        fallback=[0.90, 0.04],
        timeout=2.0,
    )
    deadline = time.monotonic() + 5.0
    while splash.exists and time.monotonic() < deadline:
        time.sleep(0.25)
    if splash.exists:
        recorder.capture("failure_kuaishou_splash_not_dismissed")
        raise automation_module.AutomationError("Kuaishou splash remained after the skip action")
    recorder.event("kuaishou_splash_dismissed_before_measurement")


def dismiss_taobao_popup(automation: Any, *, phase: str) -> bool:
    device, recorder = automation._require_ready()
    if device(resourceId="vip-container").exists:
        width, height = device.window_size()
        absolute = [int(width * 0.5), int(height * 0.773)]
        device.click(*absolute)
        recorder.event(
            "click", step="taobao_" + phase + "_popup_dismiss",
            method="vip_container_normalized_fallback",
            resource_id="vip-container", normalized=[0.5, 0.773], absolute=absolute,
        )
        return True
    selectors = [
        {"type": "description", "value": "关闭按钮"},
        {"type": "text", "value": "关闭按钮"},
    ]
    for query, selector in automation._iter_selectors(selectors):
        if query.exists:
            query.click()
            recorder.event("click", step="taobao_" + phase + "_popup_dismiss",
                           method="selector", selector=selector)
            return True
    return False


def wait_for_taobao_results(automation: Any, timeout: float) -> None:
    _device, recorder = automation._require_ready()
    submit = [
        {"type": "resource_id", "value": "com.taobao.taobao:id/searchbtn"},
        {"type": "description", "value": "搜索"},
        {"type": "text", "value": "搜索"},
    ]
    for query, selector in automation._iter_selectors(submit):
        if query.exists:
            query.click()
            recorder.event("click", step="taobao_submit_history_search",
                           method="selector", selector=selector)
            break
    results = [
        {"type": "resource_id", "value": "com.taobao.taobao:id/search_toolbar"},
        {"type": "resource_id", "value": "com.taobao.taobao:id/libsf_srp_list_blank"},
    ]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if dismiss_taobao_popup(automation, phase="result"):
            time.sleep(0.5)
        for query, _selector in automation._iter_selectors(results):
            if query.exists:
                return
        time.sleep(0.25)
    recorder.capture("failure_taobao_result_marker_after_popup_compat")
    raise automation_module.AutomationError("Taobao results did not appear after popup handling")


def position_seconds(text: str) -> int:
    """Convert player mm:ss or hh:mm:ss text to seconds."""
    fields = text.strip().split(":")
    if len(fields) not in (2, 3) or not all(v.isdigit() for v in fields):
        raise automation_module.AutomationError("Unrecognized playback-position label")
    if any(int(v) >= 60 for v in fields[1:]):
        raise automation_module.AutomationError("Invalid playback-position label")
    value = 0
    for field in fields:
        value = value * 60 + int(field)
    return value


def reset_playback_position(automation: Any, flow: dict[str, Any], timeout: float) -> None:
    """Seek before playback instead of resuming where the previous run stopped."""
    device, recorder = automation._require_ready()
    fraction = float(flow["start_seek_fraction"])
    expected = int(flow["start_position_seconds"])
    tolerance = float(flow["position_tolerance_seconds"])
    if not 0 <= fraction <= 1 or expected < 0 or not 0 <= tolerance <= 10:
        raise ValueError("Invalid frozen playback-position configuration")
    automation.click_normalized(step="show_seek_controls", normalized=flow["landscape_player_center"])
    time.sleep(float(flow["controls_settle_seconds"]))
    node = automation.wait_for_hierarchy_node(timeout=timeout, resource_id=flow["seekbar_resource_id"])
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
    if not match:
        raise automation_module.AutomationError("Playback seekbar bounds are unavailable")
    left, top, right, bottom = map(int, match.groups())
    if right <= left or bottom <= top:
        raise automation_module.AutomationError("Playback seekbar is empty")
    device.click(int(left + (right - left - 1) * fraction), (top + bottom) // 2)
    time.sleep(float(flow["after_seek_seconds"]))
    label = automation.wait_for_hierarchy_node(timeout=timeout, resource_id=flow["position_resource_id"])
    if abs(position_seconds(label.get("text", "")) - expected) > tolerance:
        raise automation_module.AutomationError("Playback did not reach the frozen start position")
    recorder.event("playback_start_position_verified", position_seconds=expected)
    # Let player controls disappear before declaring the scene ready.
    time.sleep(float(flow["controls_hide_seconds"]))

def apply_video_config(app: dict[str, Any]) -> dict[str, Any]:
    """Copy downloaded-video configuration without modifying the caller's dictionary."""
    updated = copy.deepcopy(app)
    flow = updated["flows"]["offline_playback"]
    if not flow.get("fixed_video_title"):
        raise ValueError("Configure the frozen offline video in the private App configuration")
    flow["continue_text"] = flow["fixed_video_title"]
    activities = flow["activities"].setdefault("history_download", [])
    if DOWNLOAD_CENTER_ACTIVITY not in activities:
        activities.append(DOWNLOAD_CENTER_ACTIVITY)
    return updated


def click_iqiyi_download_tab(automation: Any, timeout: float) -> None:
    device, recorder = automation._require_ready()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        root = ET.fromstring(device.dump_hierarchy())
        width, height = device.window_size()
        for node in root.iter("node"):
            if node.get("text") != "下载" or node.get("visible-to-user") != "true":
                continue
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if not match:
                continue
            left, top, right, bottom = map(int, match.groups())
            if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                continue
            x, y = (left + right) // 2, (top + bottom) // 2
            device.click(x, y)
            recorder.event("click", step="iqiyi_download_tab", method="visible_hierarchy_node",
                           absolute=[x, y])
            return
        time.sleep(0.25)
    recorder.capture("failure_iqiyi_download_tab")
    raise automation_module.AutomationError("Visible iQIYI download tab did not appear")


def run_iqiyi_playback(
    automation: Any,
    app: dict[str, Any],
    playback_seconds: int,
) -> int:
    device, recorder = automation._require_ready()
    flow = app["flows"]["offline_playback"]
    timeout = float(flow["state_timeout_seconds"])
    activities = flow["activities"]

    automation.wait_for_portrait(timeout)
    time.sleep(float(flow["cold_start_settle_seconds"]))
    automation.click_hierarchy_text(
        step="iqiyi_startup_ad_close",
        text="关闭",
        timeout=float(flow["startup_ad_probe_seconds"]),
        required=False,
    )
    time.sleep(float(flow["after_startup_ad_seconds"]))
    profile_entry = device.app_current()
    automation.click_normalized(step="iqiyi_me_tab", normalized=flow["me_tab"])
    time.sleep(1.0)
    if device(textContains=flow["profile_marker"]).exists:
        recorder.event("iqiyi_me_tab_verified_without_retry")
    else:
        automation.click_normalized(step="iqiyi_me_tab_retry", normalized=flow["me_tab"])
        recorder.event("iqiyi_me_tab_retry_applied")
    try:
        automation.wait_for_hierarchy_node(
            timeout=timeout,
            text=flow["profile_marker"],
            contains_text=True,
        )
    except automation_module.AutomationError as exc:
        recorder.capture("iqiyi_profile_marker_absent")
        current = device.app_current()
        app_is_stable = (
            current.get("package") == app["package"]
            and current.get("activity") == profile_entry.get("activity")
            and current.get("pid") == profile_entry.get("pid")
        )
        recorder.event(
            "iqiyi_profile_marker_absent",
            marker=flow["profile_marker"],
            profile_entry=profile_entry,
            current=current,
            app_is_stable=app_is_stable,
        )
        if not app_is_stable:
            raise automation_module.AutomationError(
                "iQIYI profile marker was absent and the target App state changed: "
                f"entry={profile_entry}, current={current}"
            ) from exc
    history_activity = activities["history_download"][0]
    automation.adb.shell(
        ["am", "start", "-n", f"{app['package']}/{history_activity}"],
        timeout=15.0,
    )
    recorder.event(
        "iqiyi_history_download_activity_started",
        activity=history_activity,
    )
    automation.wait_for_activity(
        package=app["package"],
        activities=activities["history_download"],
        timeout=timeout,
    )
    click_iqiyi_download_tab(automation, timeout)
    automation.wait_for_hierarchy_node(timeout=timeout, text=flow["download_marker"])
    automation.wait_for_hierarchy_node(timeout=timeout, text=flow["fixed_video_title"])
    recorder.event("iqiyi_download_layout_settle", seconds=1.0)
    time.sleep(1.0)
    automation.click_hierarchy_text(
        step="iqiyi_continue_offline_video",
        text=flow["continue_text"],
        timeout=timeout,
    )
    try:
        automation.wait_for_activity(
            package=app["package"],
            activities=activities["player"],
            timeout=timeout,
        )
    except automation_module.AutomationError:
        recorder.capture("iqiyi_player_launch_retry")
        recorder.event("iqiyi_download_layout_settle", seconds=1.0)
        time.sleep(1.0)
        automation.click_hierarchy_text(
            step="iqiyi_continue_offline_video_retry",
            text=flow["continue_text"],
            timeout=timeout,
        )
        automation.wait_for_activity(
            package=app["package"],
            activities=activities["player"],
            timeout=max(30.0, timeout * 2.0),
        )
    time.sleep(float(flow["player_settle_seconds"]))
    automation.click_normalized(
        step="iqiyi_show_portrait_controls",
        normalized=flow["portrait_player_center"],
    )
    time.sleep(float(flow["controls_settle_seconds"]))
    automation.click_normalized(
        step="iqiyi_enter_fullscreen",
        normalized=flow["portrait_fullscreen_button"],
    )
    automation.wait_for_landscape(timeout)
    time.sleep(float(flow["fullscreen_settle_seconds"]))
    reset_playback_position(automation, flow, timeout)
    recorder.capture("iqiyi_measurement_ready")
    time.sleep(float(playback_seconds))
    current = device.app_current()
    if current.get("package") != app["package"]:
        raise automation_module.AutomationError(
            f"iQIYI left foreground during playback: {current}"
        )
    recorder.event("iqiyi_playback_complete", seconds=playback_seconds)
    return playback_seconds


def run_taobao_feed(
    automation: Any,
    app: dict[str, Any],
    swipes: int,
) -> int:
    device, recorder = automation._require_ready()
    timeout = 15.0
    flow = app["flows"]["product_feed"]
    query = flow["search_query"]
    automation.return_to_activity(app["package"], flow["home_activities"])
    screen_width, screen_height = device.window_size()

    def visible_bounds(node: ET.Element) -> tuple[int, int, int, int] | None:
        match = re.fullmatch(
            r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
            node.get("bounds", ""),
        )
        if not match or node.get("visible-to-user") != "true":
            return None
        left, top, right, bottom = map(int, match.groups())
        if (
            left < 0
            or top < 0
            or right > screen_width
            or bottom > screen_height
        ):
            return None
        if right <= left or bottom <= top:
            return None
        return left, top, right, bottom

    def hierarchy_state() -> dict[str, Any]:
        root = ET.fromstring(device.dump_hierarchy())
        query = None
        sales_node = None
        products: list[str] = []
        for node in root.iter("node"):
            if visible_bounds(node) is None:
                continue
            text = node.get("text", "")
            description = node.get("content-desc", "")
            if node.get("resource-id") == "com.taobao.taobao:id/searchEdit":
                query = text
            if description == "销量" or description.startswith("销量,"):
                sales_node = node
            if "元" in description and description not in products:
                products.append(description)
        return {
            "query": query,
            "sales_node": sales_node,
            "sales_selected": (
                sales_node is not None
                and sales_node.get("content-desc") == "销量,已选中"
            ),
            "products": products,
        }

    def click_visible_history_term(term: str) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            root = ET.fromstring(device.dump_hierarchy())
            for node in root.iter("node"):
                bounds = visible_bounds(node)
                if bounds is None:
                    continue
                if term not in {node.get("text", ""), node.get("content-desc", "")}:
                    continue
                left, top, right, bottom = bounds
                if right - left < 120:
                    continue
                x, y = (left + right) // 2, (top + bottom) // 2
                device.click(x, y)
                recorder.event(
                    "click",
                    step="taobao_visible_search_history",
                    method="visible_hierarchy_node",
                    term=term,
                    absolute=[x, y],
                    bounds=list(bounds),
                )
                return
            time.sleep(0.25)
        recorder.capture("failure_taobao_visible_search_history")
        raise automation_module.AutomationError(
            f"visible Taobao search-history term did not appear: {term}"
        )

    automation.wait_for_portrait(timeout)
    time.sleep(3.0)
    if dismiss_taobao_popup(automation, phase="home"):
        time.sleep(0.5)
    automation.click_first(
        step="taobao_search_bar",
        selectors=[{"type": "description", "value": "搜索栏"}],
        fallback=[0.40, 0.12],
        timeout=timeout,
    )
    automation.wait_for_any(
        [
            {"type": "resource_id", "value": "com.taobao.taobao:id/searchbtn"},
            {"type": "description", "value": "搜索"},
        ],
        timeout,
    )
    # Taobao may expose an off-screen duplicate history node. Only click a
    # hierarchy node whose complete bounds are inside the current display.
    click_visible_history_term(query)
    wait_for_taobao_results(automation, timeout)
    time.sleep(5.0)
    current = device.app_current()
    if current.get("package") != app["package"]:
        raise automation_module.AutomationError(
            f"Taobao left foreground after search: {current}"
        )
    result_state = hierarchy_state()
    if result_state["query"] != query or not result_state["products"]:
        recorder.capture("failure_taobao_search_results")
        raise automation_module.AutomationError(
            "Taobao did not reach the configured product-result page"
        )
    if not result_state["sales_selected"]:
        sales_node = result_state["sales_node"]
        if sales_node is None:
            recorder.capture("failure_taobao_sales_sort_missing")
            raise automation_module.AutomationError(
                "visible Taobao sales-sort control is missing"
            )
        left, top, right, bottom = visible_bounds(sales_node) or (0, 0, 0, 0)
        x, y = (left + right) // 2, (top + bottom) // 2
        device.click(x, y)
        recorder.event(
            "click",
            step="taobao_sales_sort",
            method="visible_hierarchy_node",
            absolute=[x, y],
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result_state = hierarchy_state()
        if result_state["sales_selected"] and result_state["products"]:
            break
        time.sleep(0.25)
    else:
        recorder.capture("failure_taobao_sales_sort_not_selected")
        raise automation_module.AutomationError(
            "Taobao sales sort was not confirmed as selected"
        )
    time.sleep(3.0)
    automation.click_hierarchy_text(
        step="taobao_sales_tutorial_dismiss",
        text="我知道了",
        timeout=1.0,
        required=False,
    )
    result_state = hierarchy_state()
    if not result_state["sales_selected"] or not result_state["products"]:
        recorder.capture("failure_taobao_measurement_state")
        raise automation_module.AutomationError(
            "Taobao sales-result feed was not stable before browsing"
        )
    result_activity = device.app_current().get("activity")
    recorder.capture("taobao_measurement_ready")
    initial_products = tuple(result_state["products"])
    gesture = {
        "start": [0.5, 0.78],
        "end": [0.5, 0.25],
        "duration_seconds": 0.5,
    }
    completed = 0
    for index in range(swipes):
        automation.swipe_normalized(gesture)
        completed += 1
        recorder.event("taobao_product_swipe", index=index + 1, total=swipes)
        time.sleep(3.0)
    current = device.app_current()
    if (
        current.get("package") != app["package"]
        or current.get("activity") != result_activity
    ):
        raise automation_module.AutomationError(
            f"Taobao left the product-result activity during feed browsing: {current}"
        )
    final_state = hierarchy_state()
    # The search and sort bars scroll off-screen with the product feed, so the
    # final hierarchy should be validated by activity and product content.
    if not final_state["products"]:
        recorder.capture("failure_taobao_final_result_state")
        raise automation_module.AutomationError(
            "Taobao product-result feed was absent after browsing"
        )
    if tuple(final_state["products"]) == initial_products:
        recorder.capture("failure_taobao_feed_did_not_advance")
        raise automation_module.AutomationError(
            "Taobao product feed did not advance after the requested swipes"
        )
    recorder.event(
        "taobao_feed_verified",
        search_query_confirmed_before_swipes=query,
        sales_sort_confirmed_before_swipes=True,
        result_activity=result_activity,
        initial_product_count=len(initial_products),
        final_product_count=len(final_state["products"]),
    )
    automation.return_to_activity(app["package"], app["flows"]["product_feed"]["home_activities"])
    return completed


def run_kuaishou_profile_video_stable(
    automation: Any, app: dict[str, Any], swipes: int
) -> int:
    """Open the configured Kuaishou video through its live UI node under load."""
    device, recorder = automation._require_ready()
    flow = app["flows"]["profile_video"]
    timeout = float(flow["selector_timeout_seconds"])
    transition_timeout = max(30.0, timeout * 3.0)

    dismiss_kuaishou_splash(automation)
    automation.return_to_activity(app["package"], [KUAISHOU_HOME_ACTIVITY])

    # A transient selector miss on HomeActivity must not trigger Back: that exits
    # Kuaishou and turns the entire measurement window into launcher activity.
    current = device.app_current()
    if (
        current.get("package") != app["package"]
        or current.get("activity") != KUAISHOU_HOME_ACTIVITY
    ):
        recorder.capture("failure_kuaishou_initial_home_activity")
        raise automation_module.AutomationError(
            f"unexpected Kuaishou activity after cold start: {current}"
        )
    home_selector_ready = True
    try:
        automation.wait_for_any(flow["home_navigation_selectors"], timeout)
    except automation_module.AutomationError:
        # Kuaishou occasionally renders its bottom navigation without exposing
        # App nodes to UIAutomator under CPU load. The activity/PID checks below
        # remain the authoritative guard before using the frozen coordinate.
        home_selector_ready = False
    ready = device.app_current()
    if (
        ready.get("package") != app["package"]
        or ready.get("activity") != KUAISHOU_HOME_ACTIVITY
        or ready.get("pid") != current.get("pid")
    ):
        recorder.capture("failure_kuaishou_home_process_changed")
        raise automation_module.AutomationError(
            "Kuaishou process/activity changed while waiting for home UI: "
            f"before={current}, after={ready}"
        )
    recorder.event(
        "kuaishou_home_navigation_ready",
        current=ready,
        selector_confirmed=home_selector_ready,
    )
    automation.click_first(
        step="kuaishou_me_tab",
        selectors=(flow["me_tab"]["selectors"] if home_selector_ready else []),
        fallback=flow["me_tab"]["fallback_normalized"],
        timeout=(timeout if home_selector_ready else 0.0),
    )
    try:
        automation.wait_for_any(flow["own_profile_marker"], 3.0)
    except automation_module.AutomationError:
        device.press("back")
        time.sleep(0.8)
        automation.click_first(
            step="kuaishou_me_tab_retry",
            selectors=[],
            fallback=flow["me_tab"]["fallback_normalized"],
            timeout=0.0,
        )
        automation.wait_for_any(flow["own_profile_marker"], timeout)

    automation.click_first(
        step="kuaishou_following_count",
        selectors=flow["following_count"]["selectors"],
        fallback=flow["following_count"]["fallback_normalized"],
        timeout=timeout,
    )
    following_markers = [
        *flow["following_list_marker"],
        *flow["target_account"]["selectors"],
    ]
    automation.wait_for_any(following_markers, transition_timeout)
    automation.click_first(
        step="kuaishou_target_account",
        selectors=flow["target_account"]["selectors"],
        fallback=flow["target_account"]["fallback_normalized"],
        timeout=transition_timeout,
    )
    automation.wait_for_any(flow["target_profile_marker"], transition_timeout)

    def wait_for_video_activity(timeout_seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = device.app_current()
            if (
                last.get("package") == app["package"]
                and last.get("activity") == KUAISHOU_VIDEO_ACTIVITY
                and last.get("pid")
            ):
                return last
            time.sleep(0.5)
        return last

    video_current: dict[str, Any] = {}
    for attempt in range(2):
        try:
            automation.click_first(
                step=(
                    "kuaishou_target_video"
                    if attempt == 0
                    else "kuaishou_target_video_retry"
                ),
                selectors=flow["target_video"]["selectors"],
                fallback=None,
                timeout=transition_timeout,
            )
        except automation_module.AutomationError:
            if attempt == 1:
                recorder.capture("failure_kuaishou_video_open")
                raise
            recorder.event("kuaishou_target_video_click_retry", attempt=attempt + 1)
            continue

        video_current = wait_for_video_activity(transition_timeout)
        if (
            video_current.get("package") == app["package"]
            and video_current.get("activity") == KUAISHOU_VIDEO_ACTIVITY
            and video_current.get("pid")
        ):
            break
        if attempt == 1:
            recorder.capture("failure_kuaishou_video_open")
            raise automation_module.AutomationError(
                "Kuaishou target video activity did not appear after two clicks: "
                f"{video_current}"
            )
        recorder.event(
            "kuaishou_target_video_activity_retry",
            attempt=attempt + 1,
            current=video_current,
        )

    recorder.capture("kuaishou_video_open")
    current = video_current or device.app_current()
    if (
        current.get("package") != app["package"]
        or current.get("activity") != KUAISHOU_VIDEO_ACTIVITY
    ):
        recorder.capture("failure_kuaishou_initial_video_activity")
        raise automation_module.AutomationError(
            f"unexpected Kuaishou activity after opening target video: {current}"
        )
    video_pid = current.get("pid")
    if not video_pid:
        recorder.capture("failure_kuaishou_initial_video_pid")
        raise automation_module.AutomationError(
            f"Kuaishou target video has no live main-process PID: {current}"
        )
    recorder.event("kuaishou_video_process_frozen", current=current)
    time.sleep(float(flow["initial_play_seconds"]))
    completed = 0
    for index in range(swipes):
        automation.swipe_normalized(flow["swipe"])
        time.sleep(float(flow["between_swipes_seconds"]))
        current = device.app_current()
        if (
            current.get("package") != app["package"]
            or current.get("activity") != KUAISHOU_VIDEO_ACTIVITY
            or current.get("pid") != video_pid
        ):
            recorder.capture(f"failure_kuaishou_after_swipe_{index + 1:02d}")
            raise automation_module.AutomationError(
                "Kuaishou left the target video flow after swipe "
                f"{index + 1}/{swipes}: {current}"
            )
        completed += 1
        recorder.event(
            "kuaishou_swipe_complete",
            index=index + 1,
            total=swipes,
            current=current,
        )

    current = device.app_current()
    if (
        current.get("package") != app["package"]
        or current.get("activity") != KUAISHOU_VIDEO_ACTIVITY
        or current.get("pid") != video_pid
    ):
        raise automation_module.AutomationError(
            f"Kuaishou left foreground during playback: {current}"
        )
    recorder.event("kuaishou_final_video_activity_verified", current=current)
    recorder.capture("kuaishou_after_swipes")
    automation.return_to_activity(app["package"], [KUAISHOU_HOME_ACTIVITY])
    return completed
