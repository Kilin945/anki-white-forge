"""Complete Missing Cards 的「Remove Finished」純邏輯測試。

Remove Finished 只把「已經補齊」的列從對話框清單移除(純視圖操作,不動 Anki 卡片);
還缺欄位的列留在清單上、勾也留著,等額度回來直接再按 Complete Selected。
真正會出錯的決策——「哪幾列算補齊了」「移除後 pending_notes 剩哪些」「狀態列該顯示
什麼」——被抽成不依賴 Qt 的純函式,這裡直接測。Qt 的顯示/隱藏/removeWidget 不在
測試範圍(需 Anki 的 PyQt 執行環境,無法在此 headless 驗證)。

addon 會 import Anki 的 aqt（測試環境沒有）→ 假 aqt 由 conftest.py 統一安裝。
"""

import addon  # noqa: E402  (假 aqt 已由 conftest.py 安裝)


# ── _drop_notes:移除指定 note id 後,pending_notes 剩下什麼 ────────────────────
class TestDropNotes:
    def test_drops_only_the_given_ids(self):
        notes = [{"noteId": 1}, {"noteId": 2}, {"noteId": 3}]
        kept = addon._drop_notes(notes, {1, 3})
        assert [n["noteId"] for n in kept] == [2]

    def test_empty_remove_keeps_all(self):
        notes = [{"noteId": 1}, {"noteId": 2}]
        assert addon._drop_notes(notes, set()) == notes

    def test_remove_all_yields_empty(self):
        notes = [{"noteId": 1}, {"noteId": 2}]
        assert addon._drop_notes(notes, {1, 2}) == []

    def test_preserves_order_of_survivors(self):
        notes = [{"noteId": 10}, {"noteId": 20}, {"noteId": 30}, {"noteId": 40}]
        kept = addon._drop_notes(notes, {20})
        assert [n["noteId"] for n in kept] == [10, 30, 40]

    def test_does_not_mutate_input(self):
        notes = [{"noteId": 1}, {"noteId": 2}]
        addon._drop_notes(notes, {1})
        assert [n["noteId"] for n in notes] == [1, 2]      # 原 list 不動


# ── _removal_status:移除後狀態列文字 ──────────────────────────────────────────
class TestRemovalStatus:
    def test_some_still_need_filling(self):
        s = addon._removal_status(removed=3, remaining=4)
        assert "Removed 3" in s
        assert "4 still need filling" in s
        assert "selected" in s.lower()          # 剩下的勾還在,可以直接再按 Complete

    def test_all_cleared(self):
        s = addon._removal_status(removed=5, remaining=0)
        assert "cleared" in s.lower()
        assert "still need filling" not in s

    def test_reminds_to_sync(self):
        assert "sync" in addon._removal_status(1, 2).lower()
        assert "sync" in addon._removal_status(1, 0).lower()


# ── _finished_ids:跑完一批後,哪幾列該從清單上拿掉 ─────────────────────────────
#
# 關鍵:判定依據是「重讀卡片欄位,五欄都有東西」,不是 worker 的 card_done 訊號。
# card_done 的語意是「這張處理完了」不是「這張補齊了」——某欄位生失敗時 helper
# 靜默回空字串、不 raise,照樣跑到最後 emit card_done。拿它當依據,半成品會被
# 當成完成而從清單消失(紅旗卡 Refill 就是這樣把自己的待辦清單擦掉的)。
def _note(**overrides):
    """一張補齊的卡;傳欄位名進來就把那一欄弄成缺的。"""
    note = {
        "Sentence":     "He kept a valid ticket.",
        "Audio":        "[sound:valid_tts.mp3]",
        "Front_Audio":  "[sound:valid_word.mp3]",
        "Image_Prompt": '<img src="valid.jpg">',
        "Translation":  "有效的",
        "Sentence_CN":  "他留著一張有效的票。",
    }
    note.update(overrides)
    return note


class TestFinishedIds:
    def test_complete_card_counts_as_finished(self):
        assert addon._finished_ids([1], {1: _note()}.get) == {1}

    def test_card_missing_a_field_is_not_finished(self):
        lookup = {1: _note(Translation="")}.get
        assert addon._finished_ids([1], lookup) == set()

    def test_每個欄位都會擋下(self):
        for field in ("Sentence", "Audio", "Front_Audio", "Image_Prompt",
                      "Translation", "Sentence_CN"):
            lookup = {1: _note(**{field: ""})}.get
            assert addon._finished_ids([1], lookup) == set(), f"{field} 缺了卻算完成"

    def test_placeholder_sentence_is_not_finished(self):
        lookup = {1: _note(Sentence=addon.PLACEHOLDERS[0])}.get
        assert addon._finished_ids([1], lookup) == set()

    def test_deleted_card_counts_as_finished(self):
        # 視窗開著時卡片被別處刪掉(⌘D/Browse)→ 既補不了也不用補,留在清單上只是雜訊
        assert addon._finished_ids([1], {}.get) == {1}

    def test_mixed_list_keeps_the_unfinished_ones(self):
        lookup = {1: _note(), 2: _note(Translation=""), 3: _note()}.get
        assert addon._finished_ids([1, 2, 3], lookup) == {1, 3}

    def test_empty_list(self):
        assert addon._finished_ids([], {}.get) == set()
