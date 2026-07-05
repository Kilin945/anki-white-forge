"""Complete Missing Cards 的「不要用佔位符蓋掉真句子」純邏輯測試。

事故根因：第二次補卡撞到雲端額度上限、生成失敗，舊邏輯無條件把佔位符寫回 Sentence，
蓋掉了前一次已經成功生成的真句子。真正的決策——「生成失敗時到底該不該寫佔位符」——被
抽成不依賴 Qt/網路的純函式 `_sentence_to_write`，這裡直接測。

addon 模組會 import Anki 的 aqt，測試環境沒有 → 用「萬用」假模組頂替 aqt 後 import
（同 test_backfill_remove.py 的 stub 手法）。
"""
import sys
import types

import pytest


# ── 用萬用 stub 頂替 aqt,讓 addon 能被 import ──────────────────────────────────
class _AnyMeta(type):
    def __getattr__(cls, _):
        return _Any()


class _Any(metaclass=_AnyMeta):
    """什麼都能做的替身:可被繼承、可呼叫、任何屬性存取都回另一個替身。"""
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
import addon  # noqa: E402  (the real addon module)


class TestSentenceToWrite:
    def test_generated_nonempty_wins(self):
        assert addon._sentence_to_write("old sentence", "A new sentence.", "word") == "A new sentence."

    def test_generated_empty_current_empty_writes_placeholder(self):
        result = addon._sentence_to_write("", "", "widget")
        assert result == "Please add an example sentence for 'widget'."

    def test_generated_empty_current_placeholder_writes_placeholder(self):
        current = "Please add an example sentence for 'widget'."
        result = addon._sentence_to_write(current, "", "widget")
        assert result == "Please add an example sentence for 'widget'."

    def test_generated_empty_current_real_sentence_returns_none(self):
        # This is the incident: regen failed (rate limit) but a real sentence already
        # exists — must NOT overwrite it with a placeholder.
        result = addon._sentence_to_write("The widget spins quickly.", "", "widget")
        assert result is None


class TestNeedSentenceAudio:
    def test_placeholder_never_gets_audio(self):
        ph = "Please add an example sentence for 'foo'."
        assert addon._need_sentence_audio("", ph, True) is False
        assert addon._need_sentence_audio("[sound:x.mp3]", ph, False) is False

    def test_missing_audio_with_real_sentence(self):
        assert addon._need_sentence_audio("", "A real sentence.", False) is True

    def test_rewritten_sentence_forces_regen(self):
        assert addon._need_sentence_audio("[sound:x.mp3]", "New sentence.", True) is True

    def test_existing_audio_untouched_when_sentence_unchanged(self):
        assert addon._need_sentence_audio("[sound:x.mp3]", "Same sentence.", False) is False

    def test_empty_sentence_no_audio(self):
        assert addon._need_sentence_audio("", "", False) is False
