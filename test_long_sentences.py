"""Batch Operations「Rebuild Long Sentences」section 的純邏輯測試。

Qt/mw.col 層(掃描、清除、跳轉)走手動驗證;這裡測抽出來的決策純函式。
addon 會 import aqt → 用萬用假模組頂替後 import(同 test_backfill_remove.py)。
"""
import sys
import types

import pytest


class _AnyMeta(type):
    def __getattr__(cls, _):
        return _Any()


class _Any(metaclass=_AnyMeta):
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, _):
        return _Any()


def _install_fake_aqt():
    aqt = types.ModuleType("aqt")
    aqt.mw = _Any()
    qt = types.ModuleType("aqt.qt")
    for name in ["QAction", "QDialog", "QVBoxLayout", "QHBoxLayout", "QFormLayout",
                 "QLabel", "QLineEdit", "QPushButton", "QProgressBar", "QScrollArea",
                 "QTreeWidget", "QTreeWidgetItem", "QWidget", "QFrame", "QCheckBox",
                 "QKeySequenceEdit", "QKeySequence", "QMessageBox", "QThread",
                 "pyqtSignal", "Qt"]:
        setattr(qt, name, _Any)
    aqt.qt = qt
    utils = types.ModuleType("aqt.utils")
    utils.showWarning = _Any()
    utils.tooltip = _Any()
    sys.modules["aqt"] = aqt
    sys.modules["aqt.qt"] = qt
    sys.modules["aqt.utils"] = utils


_install_fake_aqt()
import addon  # noqa: E402


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
    def test_exactly_three_sentence_fields(self):
        # 清且只清「句子相關」三欄 — 防手滑加欄位
        assert addon.REBUILD_CLEAR_FIELDS == ["Sentence", "Sentence_CN", "Audio"]
