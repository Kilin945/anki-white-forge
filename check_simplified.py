#!/usr/bin/env python3
"""掃描牌組裡 Translation／Sentence_CN 含簡體獨有字的卡（唯讀；`--fix` 才寫回）。

    uv run python check_simplified.py          # 只列出，不改
    uv run python check_simplified.py --fix    # 寫回繁體（瀏覽視窗選著的卡會略過）

判斷用 core.zh_chars 的「簡體獨有字」表，不是 OpenCC s2t（見 CLAUDE.md）。
需 Anki 開著並啟用 AnkiConnect。
"""
import sys
import time

import requests

from core.anki import anki, DECK_NAME
from core.text import strip_html
from core.zh_chars import has_simplified, simplified_chars, to_traditional

FIELDS = ["Translation", "Sentence_CN"]
REREAD_WAIT = 20      # 秒；瀏覽視窗的編輯器可能晚幾秒把舊值寫回，t+0 讀到不算數（見 CLAUDE.md「Running」）


def fixed_value(raw):
    """只換表中的簡體字；HTML 標籤是 ASCII，不受影響。"""
    return to_traditional(raw)


def find_hits(notes):
    """回傳 [{nid, word, field, chars, old, new}]：欄位含簡體獨有字的卡。"""
    hits = []
    for n in notes:
        word = strip_html(n["fields"].get("Front", {}).get("value", ""))
        for field in FIELDS:
            raw = n["fields"].get(field, {}).get("value", "")
            if has_simplified(strip_html(raw)):
                hits.append({"nid": n["noteId"], "word": word, "field": field,
                             "chars": "".join(simplified_chars(strip_html(raw))),
                             "old": raw, "new": fixed_value(raw)})
    return hits


def main(argv):
    fix = "--fix" in argv
    nids = anki("findNotes", query=f'deck:"{DECK_NAME}"')
    notes = anki("notesInfo", notes=nids)
    hits = find_hits(notes)
    print(f"Scanned {len(notes)} notes; {len(hits)} field(s) contain Simplified-only characters.")
    for h in hits:
        print(f'- {h["word"]} [{h["field"]}] chars: {h["chars"]}\n    now : {h["old"]}\n    fix : {h["new"]}')
    if not hits or not fix:
        if hits:
            print("\nRead-only run. Add --fix to write the corrections.")
        return 0

    selected = set(anki("guiSelectedNotes"))          # 瀏覽視窗選著的卡寫不進去（編輯器會把舊值寫回）
    todo = [h for h in hits if h["nid"] not in selected]
    for h in hits:
        if h["nid"] in selected:
            print(f'SKIP {h["word"]} [{h["field"]}]: selected in the Browse window. '
                  f'Move the selection away (e.g. guiBrowse to an empty query) and re-run.')
    for h in todo:
        anki("updateNoteFields", note={"id": h["nid"], "fields": {h["field"]: h["new"]}})
    if not todo:
        return 1 if hits else 0
    print(f"Wrote {len(todo)} field(s). Re-reading in {REREAD_WAIT}s to verify...")
    time.sleep(REREAD_WAIT)
    now = {n["noteId"]: n for n in anki("notesInfo", notes=[h["nid"] for h in todo])}
    bad = [h for h in todo if now[h["nid"]]["fields"][h["field"]]["value"] != h["new"]]
    for h in bad:
        print(f'FAILED to stick: {h["word"]} [{h["field"]}]')
    print("Verified." if not bad else f"{len(bad)} field(s) reverted.")
    return 1 if bad or len(todo) < len(hits) else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except requests.exceptions.ConnectionError:
        print("Anki is not running or AnkiConnect is disabled.")
        sys.exit(2)
