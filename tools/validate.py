"""Run the hardware-free validation suite from any working directory."""

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    commands = [
        [sys.executable, "-m", "ruff", "check", "src", "tests", "tools"],
        [sys.executable, "-m", "pyright", "src/powermonitor"],
        [sys.executable, "-m", "pytest", "-q", "tests"],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=root, env=env, check=False)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
