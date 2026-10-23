"""Validate PerfDog window bounds and click coordinates."""


def validate_ui_config(config: dict) -> None:
    layout = config["layout"]
    screen, bounds = layout["screen_size"], layout["window_bounds"]
    if (not isinstance(screen, list) or len(screen) != 2
            or any(type(v) is not int or v <= 0 for v in screen)):
        raise ValueError("screen_size must contain positive integer width and height")
    if (not isinstance(bounds, list) or len(bounds) != 4
            or any(type(v) is not int or v < 0 for v in bounds)
            or min(bounds[2:]) <= 0 or bounds[0] + bounds[2] > screen[0]
            or bounds[1] + bounds[3] > screen[1]):
        raise ValueError("PerfDog window bounds must fit the desktop")
    for name in ("SELECT_APP", "SEARCH_BOX", "APP_ROW", "RECORD_BUTTON",
                 "SAVE_BUTTON", "EXPORT_NAME", "EXPORT_CONFIRM", "CLOSE_TEST"):
        point = layout["points"][name]
        if (not isinstance(point, list) or len(point) != 2
                or any(type(v) is not int for v in point)
                or not bounds[0] <= point[0] < bounds[0] + bounds[2]
                or not bounds[1] <= point[1] < bounds[1] + bounds[3]):
            raise ValueError("PerfDog control is outside the configured window: " + name)
