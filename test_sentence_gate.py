"""句子失敗擋下游（gate）+ 造句思考等級的 addon 端純邏輯測試。

規則：句子不可用（空 / 佔位符）時，依賴句意的下游（Image / Translation /
Sentence_CN / 句音）全部跳過，只做與句子無關的 Front_Audio；下次 ⌘S 一起重來。
判斷抽成純函式 `_sentence_usable`。造句呼叫走 effort="medium"（其餘預設 low）。

addon 模組會 import Anki 的 aqt，測試環境沒有 → 用「萬用」假模組頂替 aqt 後 import
（同 test_backfill_guards.py 的 stub 手法）。
"""
import sys
import types
from unittest.mock import patch

import pytest


# ── 用萬用 stub 頂替 aqt,讓 addon 能被 import ──────────────────────────────────
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


class TestSentenceUsable:
    def test_real_sentence_is_usable(self):
        assert addon._sentence_usable("The cat sat on the mat.") is True

    def test_empty_is_not_usable(self):
        assert addon._sentence_usable("") is False

    def test_placeholder_is_not_usable(self):
        for p in addon.PLACEHOLDERS:
            assert addon._sentence_usable(f"xx {p} yy") is False

    def test_addon_failure_placeholder_is_not_usable(self):
        # ⌘A / ⌘S 生成失敗時寫入的實際佔位符句
        assert addon._sentence_usable(
            "Please add an example sentence for 'cat'.") is False


class _RecorderDispatcher:
    def __init__(self, reply="ok"):
        self.providers = ["x"]
        self.kwargs = None
        self._reply = reply

    def generate(self, prompt, **kw):
        self.kwargs = kw
        return self._reply

    def wall_secs(self):
        return 0.0


class TestEffortCallSites:
    def test_groq_chat_defaults_to_low(self):
        rec = _RecorderDispatcher()
        with patch.object(addon, "_dispatcher", rec):
            addon._groq_chat("p", temperature=0, max_tokens=8, timeout=5)
        assert rec.kwargs["effort"] == "low"

    def test_groq_chat_forwards_effort(self):
        rec = _RecorderDispatcher()
        with patch.object(addon, "_dispatcher", rec):
            addon._groq_chat("p", temperature=0, max_tokens=8, timeout=5,
                             effort="medium")
        assert rec.kwargs["effort"] == "medium"

    def test_sentence_generation_uses_medium_effort(self):
        rec = _RecorderDispatcher(reply="A cat sat on the warm mat.")
        with patch.object(addon, "_dispatcher", rec):
            addon.Worker._groq_sentence(None, "cat")
        assert rec.kwargs["effort"] == "medium"

    def test_word_translation_stays_low_effort(self):
        rec = _RecorderDispatcher(reply="貓")
        with patch.object(addon, "_dispatcher", rec):
            addon.Worker._groq_translate(None, "cat", "A cat sat.")
        assert rec.kwargs["effort"] == "low"
