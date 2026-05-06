from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    project_root = Path(__file__).resolve().parent
    src_dir = project_root / "src"
    sys.path.insert(0, str(src_dir))

    from dark_forums.__main__ import main as pkg_main

    pkg_main()


if __name__ == "__main__":
    main()
