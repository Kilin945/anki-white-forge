"""Normalize existing Front fields in My_Daily_English.

Rule: always strip residual HTML; then lowercase the word, EXCEPT all-uppercase
acronyms (e.g. ASAP, GDP) which keep their case. New words added via ⌘D / add_word.py
are already clean — this fixes legacy cards (mostly typed on AnkiMobile, which wraps
content in <div>/<span> and doesn't lowercase).

Usage:
  uv run python normalize_fronts.py            # preview only (no writes)
  uv run python normalize_fronts.py --apply    # actually apply the changes
"""
import sys

from core.anki import anki
from core.text import strip_html


def normalized(raw):
    """Cleaned Front: HTML stripped; lowercased unless it's an all-caps acronym."""
    cleaned = strip_html(raw).strip()
    if not cleaned:
        return raw
    return cleaned if cleaned.isupper() else cleaned.lower()


def main():
    apply = "--apply" in sys.argv

    ids = anki("findNotes", query="deck:My_Daily_English")
    info = anki("notesInfo", notes=ids)

    changes, acronyms = [], []
    for n in info:
        raw = n["fields"]["Front"]["value"]
        new = normalized(raw)
        cleaned = strip_html(raw).strip()
        if cleaned and cleaned.isupper():
            acronyms.append(cleaned)
        if new != raw:
            changes.append((n["noteId"], raw, new))

    print(f"掃描 {len(info)} 張卡片 → 需要正規化 {len(changes)} 張")
    for _, old, new in changes:
        print(f"  {old!r}  →  {new!r}")
    if acronyms:
        print(f"保留的全大寫縮寫 ({len(set(acronyms))}): {', '.join(sorted(set(acronyms)))}")

    if not changes:
        print("沒有需要修改的卡片。")
        return
    if not apply:
        print("\n(預覽模式) 確認無誤後，加 --apply 實際寫入。")
        return

    for nid, _, new in changes:
        anki("updateNoteFields", note={"id": nid, "fields": {"Front": new}})
    print(f"\n✓ 已更新 {len(changes)} 張卡片。記得同步 Anki。")


if __name__ == "__main__":
    main()
