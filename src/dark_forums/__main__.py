from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_settings
from .diagnostics import run_test_notify
from .runner import run_daily


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run-daily")
    subparsers.add_parser("test-notify")
    args = parser.parse_args()

    command = args.command or "run-daily"
    if command == "test-notify":
        raise SystemExit(run_test_notify(project_root))

    settings = load_settings(project_root)
    run_daily(settings, project_root)


if __name__ == "__main__":
    main()
