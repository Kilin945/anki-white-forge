"""Batch Operations「Find Duplicate Words」section 的純邏輯測試。

Qt 層（勾選框真的點得到、刪除、確認視窗）在 check_qt_runtime.py 用真 Qt 驗；
這裡測抽出來的決策純函式。假 aqt 由 conftest.py 統一安裝。
"""

import addon  # 假 aqt 已由 conftest.py 安裝


class TestDuplicateGroups:
    def test_groups_same_word(self):
        assert addon._duplicate_groups([(1, "across"), (2, "across"), (3, "bridge")]) == \
            [("across", [1, 2])]

    def test_ignores_case_and_html(self):
        # 手機加的卡常帶 HTML 或大寫
        got = addon._duplicate_groups([(1, "across"), (2, "<b>Across</b>"), (3, " ACROSS ")])
        assert got == [("across", [1, 2, 3])]

    def test_no_duplicates(self):
        assert addon._duplicate_groups([(1, "a"), (2, "b")]) == []

    def test_blank_fronts_are_not_a_group(self):
        assert addon._duplicate_groups([(1, ""), (2, "<br>"), (3, None)]) == []

    def test_sorted_by_word(self):
        got = addon._duplicate_groups([(1, "zoo"), (2, "zoo"), (3, "ant"), (4, "ant")])
        assert [k for k, _ in got] == ["ant", "zoo"]


class TestFullyCheckedGroup:
    GROUPS = [("across", [1, 2]), ("zoo", [3, 4, 5])]

    def test_partial_check_is_fine(self):
        assert addon._fully_checked_group(self.GROUPS, {2, 3, 4}) is None

    def test_whole_group_checked_is_reported(self):
        assert addon._fully_checked_group(self.GROUPS, {1, 2}) == "across"

    def test_nothing_checked(self):
        assert addon._fully_checked_group(self.GROUPS, set()) is None
