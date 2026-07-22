from __future__ import annotations

import zipapp
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
TARGET = ROOT / "dist" / "score-fourfold.pyz"


def main() -> None:
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.unlink(missing_ok=True)
    zipapp.create_archive(
        SOURCE,
        target=TARGET,
        interpreter="/usr/bin/env python3",
        main="score_fourfold.cli:main",
        compressed=True,
        filter=lambda path: "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"},
    )
    print(f"已生成：{TARGET}")


if __name__ == "__main__":
    main()
