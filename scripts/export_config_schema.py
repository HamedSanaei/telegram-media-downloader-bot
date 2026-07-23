#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

from telegram_media_bot.bootstrap.config import Settings

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "config.schema.json"


def main() -> None:
    OUTPUT.write_text(
        json.dumps(Settings.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
