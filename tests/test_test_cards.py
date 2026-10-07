"""Batch Operations 面板「Test Cards」section 的純邏輯測試。

section 本體(建/清卡、按鈕)要動 mw.col + Qt,無法在此 headless 測;真正容易寫錯的
是「Count 欄輸入 → 幾張卡」的解析與夾限,抽成純函式 _clamp_test_count 在這裡測。

addon 會 import Anki 的 aqt（測試環境沒有）→ 假 aqt 由 conftest.py 統一安裝。
"""
import addon  # 假 aqt 已由 conftest.py 安裝


MAX = len(addon.TEST_CARD_WORDS)


class TestClampTestCount:
    def test_plain_number(self):
        assert addon._clamp_test_count("3") == 3

    def test_blank_uses_default(self):
        assert addon._clamp_test_count("") == 7

    def test_non_numeric_uses_default(self):
        assert addon._clamp_test_count("abc") == 7

    def test_strips_whitespace(self):
        assert addon._clamp_test_count("  5  ") == 5

    def test_zero_clamps_to_one(self):
        assert addon._clamp_test_count("0") == 1

    def test_negative_clamps_to_one(self):
        assert addon._clamp_test_count("-4") == 1

    def test_over_max_clamps_to_max(self):
        assert addon._clamp_test_count("999") == MAX

    def test_exactly_max_ok(self):
        assert addon._clamp_test_count(str(MAX)) == MAX


class TestWordsAreUsable:
    def test_enough_words_for_default(self):
        assert MAX >= 7                      # 預設 7 張要有足夠假詞

    def test_all_words_pass_english_gate(self):
        # 每個假詞都要能通過 _looks_english,否則會被 dialog 標成非英文、不能勾
        for word, _assoc in addon.TEST_CARD_WORDS:
            assert addon._looks_english(word), f"{word!r} 不被 _looks_english 接受"
