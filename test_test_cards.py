"""Batch Operations 面板「Test Cards」section 的純邏輯測試。

section 本體(建/清卡、按鈕)要動 mw.col + Qt,無法在此 headless 測;真正容易寫錯的
是「Count 欄輸入 → 幾張卡」的解析與夾限,抽成純函式 _clamp_test_count 在這裡測。

addon 會 import aqt,測試環境沒有 → 用萬用假模組頂替後 import(同 test_groq_limiter)。
"""
import sys
import types


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
