from __future__ import annotations

import json
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


class AdbError(RuntimeError):
    pass


@dataclass
class AdbCommandRecord:
    argv: list[str]
    started_at: float
    duration_seconds: float
    returncode: int
    stdout: str
    stderr: str


class AdbClient:
    """ADB wrapper that always binds commands to one explicit device serial."""

    def __init__(self, serial: str, adb_path: str = "adb") -> None:
        if not serial.strip():
            raise ValueError("ADB serial must not be empty")
        self.serial = serial.strip()
        self.adb_path = adb_path
        self.history: list[AdbCommandRecord] = []

    def run(
        self,
        args: Sequence[str],
        *,
        timeout: float = 30.0,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = [self.adb_path, "-s", self.serial, *map(str, args)]
        started_at = time.time()
        started_perf = time.perf_counter()
        try:
            result = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.perf_counter() - started_perf
            self.history.append(
                AdbCommandRecord(
                    argv=argv,
                    started_at=started_at,
                    duration_seconds=duration,
                    returncode=-1,
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "command timed out",
                )
            )
            raise AdbError(f"ADB command timed out after {timeout:.1f}s: {argv}") from exc

        duration = time.perf_counter() - started_perf
        self.history.append(
            AdbCommandRecord(
                argv=argv,
                started_at=started_at,
                duration_seconds=duration,
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        )
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown ADB error"
            raise AdbError(f"ADB command failed ({result.returncode}): {detail}")
        return result

    def shell(
        self,
        args: Sequence[str],
        *,
        timeout: float = 30.0,
        check: bool = True,
    ) -> str:
        return self.run(["shell", *args], timeout=timeout, check=check).stdout.strip()

    def get_state(self) -> str:
        return self.run(["get-state"], timeout=10.0).stdout.strip()

    def getprop(self, name: str) -> str:
        return self.shell(["getprop", name], timeout=10.0)

    def get_selinux_mode(self) -> str:
        return self.shell(["getenforce"], timeout=10.0)

    def set_permissive(self) -> str:
        self.shell(["setenforce", "0"], timeout=10.0)
        mode = self.get_selinux_mode()
        if mode.lower() != "permissive":
            raise AdbError(f"failed to enter SELinux permissive mode: {mode}")
        return mode

    def package_path(self, package: str) -> str:
        return self.shell(["pm", "path", package], timeout=15.0)

    def package_version(self, package: str) -> dict[str, str]:
        output = self.shell(["dumpsys", "package", package], timeout=30.0)
        values: dict[str, str] = {}
        for line in output.splitlines():
            stripped = line.strip()
            for key in ("versionCode", "versionName"):
                prefix = f"{key}="
                if stripped.startswith(prefix) and key not in values:
                    values[key] = stripped[len(prefix) :].split()[0]
        return values

    def framework_probe(self) -> dict[str, str]:
        return {
            "wm_size": self.shell(["wm", "size"], timeout=15.0),
            "wm_density": self.shell(["wm", "density"], timeout=15.0),
        }

    def write_history(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([asdict(item) for item in self.history], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
