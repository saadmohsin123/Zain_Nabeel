#!/usr/bin/env python3
"""Run all Messenger bot test suites."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], label: str) -> int:
    print(f"\n{'=' * 60}\n{label}\n{'=' * 60}")
    proc = subprocess.run(cmd, cwd=ROOT)
    return proc.returncode


def main() -> int:
    suites = [
        (["python3", "test_messenger_flow.py"], "Core flow tests"),
        (["python3", "test_messenger_aspects.py"], "Aspect tests"),
    ]
    for cmd, label in suites:
        code = run(cmd, label)
        if code != 0:
            return code

    print("\nLocal suites passed. Running live production regression...")
    return run(["python3", "scripts/live_regression_test.py"], "Live multi-scenario regression")


if __name__ == "__main__":
    sys.exit(main())
