"""Start zTT with frozen weights and save run logs."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from main_protocol import (
    DURATION_S, BACKGROUND_STARTUP_ALLOWANCE_S, BACKGROUND_CLEANUP_ALLOWANCE_S,
)

FIRST_ACTION_TIMEOUT_S = 40


def command(config: dict[str, Any], *, training: bool = False,
            beta: int = 2, duration_s: int = DURATION_S) -> list[str]:
    if beta not in (1, 2, 4) or (not training and beta != 2):
        raise ValueError("Frozen evaluation requires beta=2; training accepts beta=1, 2 or 4")
    # Include startup and cleanup time around the recording window.
    evaluation_timeout = (duration_s + FIRST_ACTION_TIMEOUT_S
                          + BACKGROUND_STARTUP_ALLOWANCE_S + BACKGROUND_CLEANUP_ALLOWANCE_S)
    result = [
        config["python"], str(Path(config["source_dir"]).resolve() / "ztt.py"),
        "--total_timesteps", "1000000000",
        "--timeOut", str(900 if training else evaluation_timeout),
        "--initSleep", "0", "--tempSet", "-1.0", "--targetTemp", "65",
        "--beta", str(beta), "--seed", "1",
        "--learning_starts", "150", "--buffer_size", "500", "--batch_size", "64",
        "--learning_rate", "0.05", "--gamma", "0.99", "--train_frequency", "10",
    ]
    if not training:
        result += ["--loadModel", config["model_stem"]]
    return result


class ZttSession:
    def __init__(self, config: dict[str, Any], directory: Path, package: str,
                 serial: str, *, training: bool = False, beta: int = 2,
                 adb: str = "adb", duration_s: int = DURATION_S) -> None:
        self.config = config
        self.directory = directory.resolve()
        self.package = package
        self.serial = serial
        self.training = training
        self.beta = beta
        self.adb = adb
        self.duration_s = duration_s
        self.process: subprocess.Popen | None = None
        self.handles: list[Any] = []
        self.checkpoints: list[str] = []
        self.offset = 0

    def actions(self) -> list[int]:
        path = self.directory / "ztt.stdout.log"
        if not path.exists():
            return []
        return [int(a) for a in re.findall(
            r"FROZEN_EVAL_ACTION\s+step=\d+\s+action=(\d+)",
            path.read_text(encoding="utf-8", errors="replace"))]

    def start(self) -> None:
        source_dir = Path(self.config["source_dir"]).resolve()
        for name in ("ztt.py", "ztt_env.py", "ztt_utils.py"):
            if not (source_dir / name).is_file():
                raise RuntimeError("Missing zTT source: " + name)
        save = self.directory / "ztt_models"
        save.mkdir(exist_ok=False)
        if not self.training:
            for suffix in ("_q_network.pt", "_target_network.pt"):
                name = self.config["model_stem"] + suffix
                if Path(name).name != name:
                    raise ValueError("Checkpoint stem must be a filename")
                source = Path(self.config["model_dir"]) / name
                if not source.is_file():
                    raise RuntimeError("Missing zTT checkpoint: " + name)
                shutil.copy2(source, save / name)
                self.checkpoints.append(name)
        env = dict(os.environ, ANDROID_SERIAL=self.serial, PYTHONUNBUFFERED="1",
                   EARDVFS_ADB=self.adb, EARDVFS_MANAGED_RUN="1",
                   EARDVFS_CONTROL_SNAPSHOT=str(self.directory / "ztt_controls_before.json"),
                   EARDVFS_TARGET_PACKAGE=self.package,
                   EARDVFS_FROZEN_EVAL="0" if self.training else "1",
                   EARDVFS_SCREEN_PREP="0", EARDVFS_BATTERY_PREP="0",
                   EARDVFS_RUNS_DIR=str(self.directory / "ztt_logs"),
                   EARDVFS_SAVE_DIR=str(save),
                   EARDVFS_PREFER_SURFACEVIEW="1", EARDVFS_FPS_RETRY_COUNT="10")
        self.handles = [(self.directory / f"ztt.{stream}.log").open("w", encoding="utf-8")
                        for stream in ("stdout", "stderr")]
        cmd = command(self.config, training=self.training, beta=self.beta,
                      duration_s=self.duration_s)
        (self.directory / "ztt_command.json").write_text(json.dumps(cmd, indent=2), encoding="utf-8")
        try:
            self.process = subprocess.Popen(cmd, cwd=source_dir.parent, env=env,
                                            stdout=self.handles[0], stderr=self.handles[1])
            deadline = time.monotonic() + FIRST_ACTION_TIMEOUT_S
            while not self.training and not self.actions():
                self.check()
                if time.monotonic() >= deadline:
                    raise RuntimeError("zTT produced no action")
                time.sleep(0.2)
            if not self.training:
                self.check()
        except BaseException:
            self.stop(validate=False)
            raise

    def mark_measurement_start(self) -> None:
        self.offset = len(self.actions())

    def check(self) -> None:
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("zTT exited before the measurement ended")

    def stop(self, *, validate: bool = True) -> dict[str, Any]:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for handle in self.handles:
            handle.close()
        actions = self.actions()[self.offset:]
        if self.training:
            return {"training": True, "budget_s": 900, "beta": self.beta}
        files_present = len(self.checkpoints) == 2 and all(
            (self.directory / "ztt_models" / name).is_file()
            and (Path(self.config["model_dir"]) / name).is_file()
            for name in self.checkpoints)
        valid = bool(actions) and all(0 <= a < 9 for a in actions) and files_present
        result = {"frozen_eval_requested": True, "weights_compared": False,
                  "checkpoint_files_present": files_present,
                  "actions": actions, "constant_action_required": False, "valid": valid}
        (self.directory / "ztt_validation.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if validate and not valid:
            raise RuntimeError("zTT checkpoint presence or action domain failed validation")
        return result
