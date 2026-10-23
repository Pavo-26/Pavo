"""Run the main experiments on Pixel 6 or OnePlus 15."""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from main_protocol import (
    APPS, DURATION_S, POLICIES, REPETITIONS, WORKERS,
    APP_ORDER, INTER_RUN_DELAY_S, CleanupError,
    select, validate_measurement, validate_runtime,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=POLICIES, required=True)
    parser.add_argument("--apps", default="all")
    parser.add_argument("--policies", default="all")
    parser.add_argument("--scenario", choices=("mixed", "no-load"), default="mixed")
    parser.add_argument("--serial")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--execute", action="store_true", required=True)
    return parser.parse_args(argv)


def make_runs(platform: str, apps: list[str], policies: list[str]) -> list[dict[str, Any]]:
    if platform not in POLICIES:
        raise ValueError("Unknown platform")
    if not apps or len(apps) != len(set(apps)) or any(a not in APPS for a in apps):
        raise ValueError("Invalid applications")
    if not policies or len(policies) != len(set(policies)) or any(p not in POLICIES[platform] for p in policies):
        raise ValueError("Invalid policies")
    # Both devices run five consecutive repetitions per App/policy.
    return [
        {"app": app, "policy": policy, "repetition": repetition}
        for app in apps for policy in policies
        for repetition in range(1, REPETITIONS + 1)
    ]


def run_window(driver: Any, *, clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Stop PerfDog 600 seconds after recording starts, then interrupt the App."""
    try:
        driver.prepare()
        recorded_at = driver.start_recording()
        deadline = recorded_at + DURATION_S
        driver.start_background()
        driver.start_interaction()
        if clock() >= deadline:
            raise RuntimeError("App startup exhausted the recording window")
        next_health_check = clock()
        while clock() < deadline:
            now = clock()
            if now >= next_health_check and deadline - now > 10:
                driver.check_health()
                next_health_check = clock() + 5
            sleep(min(0.1, max(0.0, deadline - clock())))
        # Stop capture before any blocking App shutdown or device checks.
        driver.stop_recording()
        driver.stop_interaction()
        # Stop recording before checking workload status and cleaning up.
        driver.check_health()
        result = driver.export()
        validate_measurement(result)
        return result
    finally:
        # Restoration failure also fails the run; retain its error record.
        try:
            driver.cleanup()
        except Exception as exc:
            raise CleanupError("Device cleanup failed; stop campaign") from exc


def summarize(rows: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = {(r["app"], r["policy"], r["repetition"]) for r in runs}
    actual = [(r["app"], r["policy"], r["repetition"]) for r in rows]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("Missing or duplicated repetitions")
    if any(r.get("status") != "valid" for r in rows):
        raise ValueError("Run inventory requires successful captures")
    summaries = []
    for app, policy in sorted({(r["app"], r["policy"]) for r in rows}):
        group = [r for r in rows if r["app"] == app and r["policy"] == policy]
        if len(group) != REPETITIONS:
            raise ValueError("Exactly five independent runs are required")
        summary = {"app": app, "policy": policy, "n": REPETITIONS}
        summary["raw_exports"] = [r["copied_xlsx"] for r in group]
        summary["purpose"] = "run inventory"
        summaries.append(summary)
    return summaries


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        apps = select(args.apps, APP_ORDER)
        policies = select(args.policies, POLICIES[args.platform])
        runs = make_runs(args.platform, apps, policies)
        workers = WORKERS if args.scenario == "mixed" else 0
        if not args.serial or not args.runtime_config or not args.output_root:
            raise ValueError("--execute requires --serial, --runtime-config and a new --output-root")
        config = json.loads(args.runtime_config.read_text(encoding="utf-8-sig"))
        validate_runtime(config, args.platform, policies, apps)
        if args.output_root.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError("Choose an output directory outside the source tree")
        from device_control import DeviceDriver
        args.output_root.mkdir(parents=True, exist_ok=False)
        rows = []
        for index, run in enumerate(runs, 1):
            directory = args.output_root / f"{index:03d}_{run['app']}_{run['policy']}_r{run['repetition']}"
            directory.mkdir()
            row = dict(run, status="failed")
            try:
                driver = DeviceDriver(
                    platform=args.platform, run=run, config=config,
                    directory=directory, serial=args.serial, adb=args.adb,
                    workers=workers,
                )
                row.update(run_window(driver), status="valid")
            except BaseException as exc:
                row["error"] = str(exc)
                raise
            finally:
                write_json(directory / "capture.json", row)
                rows.append(row)
                write_json(args.output_root / "run_inventory.json", rows)
            if (index < len(runs)
                    and (runs[index]["app"], runs[index]["policy"])
                    == (run["app"], run["policy"])):
                time.sleep(INTER_RUN_DELAY_S)
        inventory = summarize(rows, runs)
        write_json(args.output_root / "capture_inventory.json", inventory)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
