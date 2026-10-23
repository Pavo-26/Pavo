"""Train zTT on Pixel 6."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "experiments"))
from main_protocol import APPS, WORKERS, validate_runtime  # noqa: E402
from ztt_runner import ZttSession, command  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", choices=APPS, required=True)
    parser.add_argument("--beta", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--serial")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--execute", action="store_true", required=True)
    args = parser.parse_args(argv)
    record = {"platform": "pixel6", "app": args.app, "beta": args.beta,
            "budget_s": 900, "workers": WORKERS, "readiness_before_policy": True}
    if not args.serial or not args.runtime_config or not args.output:
        raise ValueError("--execute requires --serial, --runtime-config and a new --output")
    config = json.loads(args.runtime_config.read_text(encoding="utf-8-sig"))
    validate_runtime(config, "pixel6", ["ztt"], [args.app], ztt_training=True)
    from device_control import DeviceDriver, RemoteProcess
    from app_runner import AppFlow
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError("Training output must be outside the source artifact")
    output.mkdir(parents=True, exist_ok=False)
    record["command"] = command(config["ztt"], training=True, beta=args.beta)
    record["completed"] = False
    driver = DeviceDriver(platform="pixel6",
                          run={"app": args.app, "policy": "ztt", "repetition": 0},
                          config=config, directory=output, serial=args.serial,
                          adb=args.adb, workers=WORKERS)
    try:
        driver.check_environment()
        driver.save_and_set_controls()
        app_config = dict(config["apps"][args.app], package=driver.package)
        driver.flow = AppFlow("pixel6", args.app, args.serial, args.adb, app_config, output)
        driver.flow.launch()
        driver.flow.start()
        driver.flow.wait_for_scene()
        driver.ready("before_background")
        driver.background = RemoteProcess(
            driver.adb, shlex.quote(config["stress_ng"]["path"]) +
            f" --cpu {WORKERS} --cpu-method matrixprod --oom-avoid --metrics-brief --times --timeout 1200s",
            output, "background")
        driver.background.start()
        time.sleep(3)
        driver.ready("after_background")
        driver.ztt = ZttSession(config["ztt"], output, driver.package, args.serial,
                                training=True, beta=args.beta, adb=args.adb)
        driver.ztt.start()
        deadline = time.monotonic() + 1080
        while driver.ztt.process.poll() is None:
            driver.flow.check()
            driver.background.check()
            driver.require_workers()
            if not driver.foreground():
                raise RuntimeError("Training app left the foreground")
            if time.monotonic() >= deadline:
                raise RuntimeError("zTT exceeded its 900-second budget plus startup allowance")
            time.sleep(1)
        if driver.ztt.process.returncode != 0:
            raise RuntimeError("zTT training process failed")
        # Check that training finished and saved both networks.
        log = (output / "ztt.stdout.log").read_text(encoding="utf-8")
        if "TRAIN_START budget_s=900" not in log or "TRAIN_COMPLETE elapsed_s=" not in log:
            raise RuntimeError("Missing training-budget completion record")
        models = sorted((output / "ztt_models").glob("*.pt"))
        if len(models) != 2 or not any(p.name.endswith("_q_network.pt") for p in models) or not any(
                p.name.endswith("_target_network.pt") for p in models):
            raise RuntimeError("Missing trained checkpoint pair")
        record["checkpoints"] = [p.name for p in models]
        record["completed"] = True
    except BaseException as exc:
        record["error"] = str(exc)
        raise
    finally:
        try:
            driver.cleanup()
        except BaseException as exc:
            record["completed"] = False
            record["cleanup_error"] = str(exc)
            raise
        finally:
            (output / "readiness.json").write_text(
                json.dumps(driver.readiness, indent=2), encoding="utf-8")
            (output / "training-record.json").write_text(json.dumps(record, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
