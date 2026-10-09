"""Batch Operations「Find Duplicate Words」section 的純邏輯測試。

Qt 層（勾選框真的點得到、刪除、確認視窗）在 check_qt_runtime.py 用真 Qt 驗；
這裡測抽出來的決策純函式。假 aqt 由 conftest.py 統一安裝。
"""

import addon  # 假 aqt 已由 conftest.py 安裝


class TestDuplicateGroups:
    def test_groups_same_word(self):
        assert addon._sec_duplicates._duplicate_groups([(1, "across"), (2, "across"), (3, "bridge")]) == \
            [("across", [1, 2])]

    def test_ignores_case_and_html(self):
        # 手機加的卡常帶 HTML 或大寫
        got = addon._sec_duplicates._duplicate_groups([(1, "across"), (2, "<b>Across</b>"), (3, " ACROSS ")])
        assert got == [("across", [1, 2, 3])]

    def test_no_duplicates(self):
        assert addon._sec_duplicates._duplicate_groups([(1, "a"), (2, "b")]) == []

    def test_blank_fronts_are_not_a_group(self):
        assert addon._sec_duplicates._duplicate_groups([(1, ""), (2, "<br>"), (3, None)]) == []

    def test_sorted_by_word(self):
        got = addon._sec_duplicates._duplicate_groups([(1, "zoo"), (2, "zoo"), (3, "ant"), (4, "ant")])
        assert [k for k, _ in got] == ["ant", "zoo"]


class TestFullyCheckedGroup:
    GROUPS = [("across", [1, 2]), ("zoo", [3, 4, 5])]

    def test_partial_check_is_fine(self):
        assert addon._sec_duplicates._fully_checked_group(self.GROUPS, {2, 3, 4}) is None

    def test_whole_group_checked_is_reported(self):
        assert addon._sec_duplicates._fully_checked_group(self.GROUPS, {1, 2}) == "across"

    def test_nothing_checked(self):
        assert addon._sec_duplicates._fully_checked_group(self.GROUPS, set()) is None


class TestDifferingFields:
    def test_only_fields_that_differ(self):
        a = {"Translation": "乏味", "Sentence_CN": "講座太無聊了", "Association": ""}
        b = {"Translation": "枯燥乏味", "Sentence_CN": "講座太無聊了", "Association": ""}
        assert addon._sec_duplicates._differing_fields([a, b]) == ["Translation"]

    def test_html_only_difference_is_not_a_difference(self):
        a = {"Translation": "<b>乏味</b>", "Sentence_CN": "", "Association": ""}
        b = {"Translation": "乏味", "Sentence_CN": "", "Association": ""}
        assert addon._sec_duplicates._differing_fields([a, b]) == []

    def test_missing_field_counts_as_empty(self):
        a = {"Translation": "乏味"}
        b = {"Translation": "乏味", "Association": "boring"}
        assert addon._sec_duplicates._differing_fields([a, b]) == ["Association"]


class TestReviewSummary:
    def test_never_reviewed(self):
        assert addon._sec_duplicates._review_summary(0, 0) == "never reviewed"

    def test_one_review_no_interval(self):
        assert addon._sec_duplicates._review_summary(1, 0) == "1 review"

    def test_reviews_with_interval(self):
        assert addon._sec_duplicates._review_summary(5, 124) == "5 reviews · 124-day interval"


class TestImageFilename:
    def test_real_field(self):
        v = '<img src="dull_img_1790187098.jpg"><div style="font-size:10px">credit</div>'
        assert addon._text._image_filename(v) == "dull_img_1790187098.jpg"

    def test_no_image(self):
        assert addon._text._image_filename("") is None
        assert addon._text._image_filename("<div>leftover</div>") is None
