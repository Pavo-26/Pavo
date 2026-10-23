"""Validate App targets, screen coordinates and navigation settings."""
from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Any

SELECTOR_KEYS = {"text", "textContains", "description", "descriptionContains", "resourceId", "className"}


def require_concrete(value: Any) -> None:
    if isinstance(value, dict):
        for child in value.values():
            require_concrete(child)
    elif isinstance(value, list):
        for child in value:
            require_concrete(child)
    elif isinstance(value, str):
        if not value.strip() or value.startswith("SET_"):
            raise ValueError("Replace every App configuration placeholder")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError("Configuration numbers must be finite")
    elif value is None:
        raise ValueError("Configuration values cannot be null")


def require_shape(value: Any, template: Any, field: str = "") -> None:
    """Check required fields and types; each device configuration supplies the values."""
    if isinstance(template, dict):
        if not isinstance(value, dict):
            raise ValueError("Expected a configuration object: " + field)
        for key, example in template.items():
            if key not in value:
                raise ValueError("Missing configuration field: " + field + "." + key)
            require_shape(value[key], example, field + "." + key)
    elif isinstance(template, list):
        if not isinstance(value, list) or not value:
            raise ValueError("Expected a nonempty configuration list: " + field)
        if all(isinstance(v, (int, float)) for v in template):
            if len(value) != len(template) or any(
                    type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                    for v in value):
                raise ValueError("Normalized coordinates must be finite and in [0, 1]")
        else:
            for child in value:
                require_shape(child, template[0], field)
    elif isinstance(template, str):
        if not isinstance(value, str):
            raise ValueError("Expected configuration text: " + field)
    elif type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError("Invalid nonnegative configuration number: " + field)


def validate_oneplus_config(config: dict, app: str, package: str) -> dict:
    record = config["apps"][app]
    if record.get("package") != package:
        raise ValueError("OnePlus configuration targets another package")
    template = json.loads((Path(__file__).parent / "configs/oneplus15_apps.example.json")
                          .read_text(encoding="utf-8"))["apps"][app]
    require_concrete({key: record[key] for key in template})
    require_shape(record, template)
    if app == "dy":
        index = record["flows"]["profile_video"]["target_video"]["index"]
        if type(index) is not int or index < 0:
            raise ValueError("Video index must be a nonnegative integer")
    if app == "ks":
        for selector in record["flows"]["profile_video"]["target_video"]["selectors"]:
            index = selector.get("index", 0)
            if type(index) is not int or index < 0:
                raise ValueError("Video index must be a nonnegative integer")
    if app == "aqy":
        playback = record["flows"]["offline_playback"]
        if not 0 <= playback["start_seek_fraction"] <= 1 or not 0 <= playback["position_tolerance_seconds"] <= 10:
            raise ValueError("Invalid frozen video start position")
    return record


def validate_selectors(selectors: Any) -> None:
    if not isinstance(selectors, list) or not selectors:
        raise ValueError("UI selectors must be a nonempty list")
    for selector in selectors:
        if not isinstance(selector, dict) or not selector or set(selector) - SELECTOR_KEYS:
            raise ValueError("Unsupported UI selector")
        if any(not isinstance(v, str) or not v or v.startswith("SET_") for v in selector.values()):
            raise ValueError("Replace every UI selector placeholder")


def validate_pixel_config(config: dict, app: str, package: str) -> dict:
    record = config["apps"][app]
    if record.get("package") != package:
        raise ValueError("Pixel configuration targets another package")
    size = record["screen_size"]
    if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
        raise ValueError("screen_size must contain positive integer width and height")
    target_keys = {
        "dy": ("target_account", "target_video"),
        "ks": ("target_account", "target_video"),
        "wb": ("search_history", "target_profile"),
    }
    for key in target_keys.get(app, ()):
        validate_selectors([record.get("targets", {}).get(key)])
    if app == "tb":
        query = record.get("search_query")
        if not isinstance(query, str) or not query.strip() or query.startswith("SET_"):
            raise ValueError("Configure the product search query")
    if app == "aqy":
        playback = record["playback"]
        title = playback.get("fixed_video_title")
        if not isinstance(title, str) or not title.strip() or title.startswith("SET_"):
            raise ValueError("Configure the offline video title")
    if app != "aqy":
        steps = record.get("return_steps")
        if not isinstance(steps, list) or not steps:
            raise ValueError("Configure the Back steps to the next round's entry")
        for step in steps:
            if step.get("key") != "back" or set(step) - {"key", "wait_s"}:
                raise ValueError("Return steps use Back and an optional wait_s")
            wait = step.get("wait_s", 1)
            if type(wait) not in (int, float) or not math.isfinite(wait) or wait < 0:
                raise ValueError("Return wait_s must be nonnegative")
    return record
