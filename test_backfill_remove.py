"""Complete Missing Cards 的「Remove Selected」純邏輯測試。

Remove Selected 只把勾選的列從對話框清單移除(純視圖操作,不動 Anki 卡片)。
真正會出錯的決策——「移除後 pending_notes 剩哪些」「狀態列該顯示什麼」——被抽成
不依賴 Qt 的純函式,這裡直接測。Qt 的顯示/隱藏/removeWidget 不在測試範圍
(需 Anki 的 PyQt 執行環境,無法在此 headless 驗證)。

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
    def test_some_still_shown(self):
        s = addon._removal_status(removed=3, remaining=4)
        assert "Removed 3" in s
        assert "4 still shown" in s

    def test_all_cleared(self):
        s = addon._removal_status(removed=5, remaining=0)
        assert "cleared" in s.lower()
        assert "still shown" not in s

    def test_reminds_to_sync(self):
        assert "sync" in addon._removal_status(1, 2).lower()
        assert "sync" in addon._removal_status(1, 0).lower()
