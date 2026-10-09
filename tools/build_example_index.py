"""第一次建好卡範例索引（之後由 ⌘S 開跑時自動增量更新）。需 Anki 開著並啟用 AnkiConnect。
用法：uv run python tools/build_example_index.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.examples import examples as ex   # noqa: E402

ANKI_URL = "http://localhost:8765"


def main():
    added, removed = ex.refresh_index(ex.anki_connect(ANKI_URL), retries=6)
    print(f"example index: +{added} -{removed}, total {len(ex.load_index())} → {ex.EXAMPLES_PATH}")


if __name__ == "__main__":
    main()
