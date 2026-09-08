"""手動測試 Complete Missing Cards 對話框(Remove Selected 等)用的測試卡工具。

建出「只有 Front + Association、其他欄位全空」的裸卡 → 它們會被 dialog 認成
「缺欄位」而出現在清單,讓你勾選、Complete、測 Remove Selected。全部打上專屬
tag,清除靠 tag 一鍵刪,不留痕跡,也不會誤刪你真正的單字卡。

Anki 要開著並啟用 AnkiConnect。用法:
    uv run python make_test_cards.py add [N]   # 建 N 張裸卡(預設 7,上限見 WORDS)
    uv run python make_test_cards.py clean     # 刪掉全部測試卡

Front 用純字母假詞(zztest 前綴):_looks_english 會擋數字,故不能用 test01 這種。
"""
import sys

from core.anki import anki, DECK_NAME, MODEL_NAME

TEST_TAG = "whiteforge_test"

# 純字母假詞(避開 _looks_english 的數字限制)+ 一句提示。zztest 前綴好認、幾乎不撞真卡。
WORDS = [
    ("zztestalpha",   "a test word"),
    ("zztestbravo",   "a test word"),
    ("zztestcharlie", "a test word"),
    ("zztestdelta",   "a test word"),
    ("zztestecho",    "a test word"),
    ("zztestfoxtrot", "a test word"),
    ("zztestgolf",    "a test word"),
    ("zztesthotel",   "a test word"),
    ("zztestindia",   "a test word"),
    ("zztestjuliet",  "a test word"),
]


def add_cards(n):
    n = max(1, min(n, len(WORDS)))
    added, skipped = 0, 0
    for word, assoc in WORDS[:n]:
        try:
            anki("addNote", note={
                "deckName": DECK_NAME,
                "modelName": MODEL_NAME,
                "fields": {
                    "Front": word,
                    "Association": assoc,
                    "Sentence": "",
                    "Image_Prompt": "",
                    "Audio": "",
                    "Front_Audio": "",
                },
                "tags": [TEST_TAG],
                "options": {"allowDuplicate": False},
            })
            added += 1
        except RuntimeError as e:
            if "duplicate" in str(e).lower():   # 已經有同名測試卡了,略過
                skipped += 1
            else:
                raise
    print(f"Added {added} test card(s)" + (f", skipped {skipped} duplicate(s)" if skipped else ""))
    print(f"→ 開 Anki 按 ⌘S(Complete Missing Cards),它們(Front 以 zztest 開頭)就會出現。")


def clean():
    ids = anki("findNotes", query=f'deck:"{DECK_NAME}" tag:{TEST_TAG}')
    if not ids:
        print("No test cards to clean.")
        return
    anki("deleteNotes", notes=ids)
    print(f"Deleted {len(ids)} test card(s).")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else ""
    if cmd == "add":
        n = int(args[1]) if len(args) > 1 else 7
        add_cards(n)
    elif cmd == "clean":
        clean()
    else:
        print("Usage:")
        print("  uv run python make_test_cards.py add [N]   # 建 N 張裸卡(預設 7)")
        print("  uv run python make_test_cards.py clean     # 刪掉全部測試卡")
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
