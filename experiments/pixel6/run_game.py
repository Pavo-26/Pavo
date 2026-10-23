"""Pixel 6 Honor of Kings capture: repeat skill cycles within a 600-second recording."""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main_protocol import (
    REPETITIONS, WORKERS, POLICIES, INTER_RUN_DELAY_S, select, validate_runtime,
)
from run_main import run_window, summarize, write_json

APP = "honor_of_kings"
GAME_VERSION = "11.4.1.1"


def make_game_runs(policies):
    if (not policies or len(policies) != len(set(policies))
            or any(p not in POLICIES["pixel6"] for p in policies)):
        raise ValueError("Invalid game policy selection")
    return [
        {"app": APP, "policy": policy, "repetition": repetition}
        for policy in policies for repetition in range(1, REPETITIONS + 1)
    ]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policies", default="all")
    parser.add_argument("--scenario", choices=("mixed", "no-load"), default="mixed")
    parser.add_argument("--serial")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--runtime-config", type=Path)
    parser.add_argument("--output-root", type=Path)
    parser.add_argument("--execute", action="store_true", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        policies = select(args.policies, POLICIES["pixel6"])
        runs = make_game_runs(policies)
        workers = WORKERS if args.scenario == "mixed" else 0
        if not args.serial or not args.runtime_config or not args.output_root:
            raise ValueError("--execute requires --serial, --runtime-config and a new --output-root")
        config = json.loads(args.runtime_config.read_text(encoding="utf-8-sig"))
        validate_runtime(config, "pixel6", policies, [APP])
        if config["apps"][APP]["version_name"] != GAME_VERSION:
            raise ValueError("The Pixel game experiment uses Honor of Kings v" + GAME_VERSION)
        if args.output_root.resolve().is_relative_to(Path(__file__).resolve().parents[2]):
            raise ValueError("Results must be outside the source package")
        # Device and desktop automation are loaded only for execution.
        from pixel6.honor_of_kings import GameDriver, load_game_config
        load_game_config(config["apps"][APP]["ui_config"])
        args.output_root.mkdir(parents=True, exist_ok=False)
        rows = []
        logger = logging.getLogger("pavo.game")
        logger.setLevel(logging.INFO)
        for index, run in enumerate(runs, 1):
            directory = args.output_root / f"{index:03d}_{APP}_{run['policy']}_r{run['repetition']}"
            directory.mkdir()
            handler = logging.FileHandler(directory / "game.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)
            row = dict(run, status="failed")
            try:
                driver = GameDriver(
                    platform="pixel6", run=run, config=config, directory=directory,
                    serial=args.serial, adb=args.adb, workers=workers,
                )
                row.update(run_window(driver), status="valid")
            except BaseException as exc:
                row["error"] = str(exc)
                raise
            finally:
                logger.removeHandler(handler)
                handler.close()
                write_json(directory / "capture.json", row)
                rows.append(row)
                write_json(args.output_root / "run_inventory.json", rows)
            if index < len(runs) and runs[index]["policy"] == run["policy"]:
                time.sleep(INTER_RUN_DELAY_S)
        write_json(args.output_root / "capture_inventory.json", summarize(rows, runs))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
