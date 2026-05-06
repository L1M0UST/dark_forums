from __future__ import annotations

from pathlib import Path

from .config import load_settings
from .runner import run_daily


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    settings = load_settings(project_root)
    run_daily(settings, project_root)


if __name__ == "__main__":
    main()
