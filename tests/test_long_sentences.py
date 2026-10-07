"""Batch Operations「Rebuild Long Sentences」section 的純邏輯測試。

Qt/mw.col 層(掃描、清除、跳轉)走手動驗證;這裡測抽出來的決策純函式。
addon 會 import Anki 的 aqt（測試環境沒有）→ 假 aqt 由 conftest.py 統一安裝。
"""

import addon  # 假 aqt 已由 conftest.py 安裝


class TestSentenceWordCount:
    def test_plain_sentence(self):
        assert addon._sentence_word_count("The quick brown fox jumps.") == 5

    def test_strips_html(self):
        assert addon._sentence_word_count("<div>The <b>quick</b> fox.</div>") == 3

    def test_empty_is_zero(self):
        assert addon._sentence_word_count("") == 0
        assert addon._sentence_word_count("   ") == 0

    def test_placeholder_is_zero(self):
        # 佔位符=「缺句」不是「長句」,回 0 → 永遠不超標
        assert addon._sentence_word_count(
            "Please add an example sentence for 'foo'.") == 0

    def test_long_sentence(self):
        s = " ".join(["word"] * 27)
        assert addon._sentence_word_count(s) == 27


class TestClampLengthThreshold:
    def test_plain_number(self):
        assert addon._clamp_length_threshold("16") == 16

    def test_blank_uses_default(self):
        assert addon._clamp_length_threshold("") == 20

    def test_non_numeric_uses_default(self):
        assert addon._clamp_length_threshold("abc") == 20

    def test_zero_and_negative_clamp_to_one(self):
        assert addon._clamp_length_threshold("0") == 1
        assert addon._clamp_length_threshold("-5") == 1

    def test_big_number_accepted(self):
        assert addon._clamp_length_threshold("999") == 999   # 掃不到東西是合理結果

    def test_strips_whitespace(self):
        assert addon._clamp_length_threshold("  18  ") == 18


class TestLongSentenceLabel:
    def test_format(self):
        assert addon._long_sentence_label("transient", 27) == "transient(27)"


class TestRebuildClearFields:
    def test_rebuild_clears_everything_except_word_level(self):
        # 換句=全重建:清句子三欄+單字翻譯+圖(使用者實測後定案 —
        # Translation 依句中用法翻、Image 依句意搜,換句都該重來)
        assert addon.REBUILD_CLEAR_FIELDS == [
            "Sentence", "Sentence_CN", "Audio", "Translation", "Image_Prompt"]

    def test_word_level_fields_never_cleared(self):
        for kept in ("Front", "Association", "Front_Audio"):
            assert kept not in addon.REBUILD_CLEAR_FIELDS
