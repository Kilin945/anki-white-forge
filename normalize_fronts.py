"""Normalize existing Front fields in My_Daily_English.

Rule: always strip residual HTML; then lowercase the word, EXCEPT all-uppercase
acronyms (e.g. ASAP, GDP) which keep their case. New words added via ⌘D / add_word.py
are already clean — this fixes legacy cards (mostly typed on AnkiMobile, which wraps
content in <div>/<span> and doesn't lowercase).

Collision guard: if normalizing would make two cards share the same Front (e.g.
"<div>audit</div>" and an existing "audit"), those changes are skipped and reported —
resolve the real duplicate manually first.

Usage:
  uv run python normalize_fronts.py            # preview only (no writes)
  uv run python normalize_fronts.py --apply    # actually apply the changes
"""
import sys
from collections import Counter

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

    ids = anki("findNotes", query='deck:My_Daily_English note:English_White_Method')
    info = anki("notesInfo", notes=ids)

    # collision guard: a normalized value shared by >1 note would create duplicate Fronts
    counts = Counter(normalized(n["fields"]["Front"]["value"]) for n in info)
    collisions = {v for v, c in counts.items() if c > 1}

    changes, skipped, acronyms = [], [], []
    for n in info:
        raw = n["fields"]["Front"]["value"]
        new = normalized(raw)
        cleaned = strip_html(raw).strip()
        if cleaned and cleaned.isupper():
            acronyms.append(cleaned)
        if new == raw:
            continue
        if new in collisions:
            skipped.append((raw, new))
        else:
            changes.append((n["noteId"], raw, new))

    print(f"掃描 {len(info)} 張卡片 → 可正規化 {len(changes)} 張")
    for _, old, new in changes:
        print(f"  {old!r}  →  {new!r}")
    if skipped:
        print(f"\n⚠ 跳過 {len(skipped)} 張（正規化後會與其他卡片重複，請先手動處理）：")
        for old, new in skipped:
            print(f"  {old!r}  →  {new!r}（會撞到既有的 {new!r}）")
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
