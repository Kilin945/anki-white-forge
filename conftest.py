"""pytest 共用設定。

兩件事：
1. 假 aqt —— `addon/__init__.py` 跑在 Anki 的 Python，會 import `aqt`（測試環境沒有）。
   這裡用「萬用替身」頂替後，測試檔直接 `import addon` 就好，不用各自抄一份 stub
   （曾經 7 個測試檔逐字重複同一份，addon 多 import 一個 Qt 類別就要改 7 次）。
   conftest 保證比測試模組先載入，所以 module 層安裝即可。
2. 測試隔離 —— dispatcher 測試會驅動真的 Dispatcher，其 failover/429 WARNING 若不攔截
   會寫進正式的 logs/addon_llm.log（出現過假事故 "groq failed (x)" 污染鑑識資料）。
"""
import logging
import sys
import types

import pytest


# ── 假 aqt ────────────────────────────────────────────────────────────────────

class _AnyMeta(type):
    def __getattr__(cls, _):
        return _Any()


class _Any(metaclass=_AnyMeta):
    """什麼都能做的替身：可被繼承、可呼叫、任何屬性存取都回另一個替身。"""
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, _):
        return _Any()


def install_fake_aqt():
    aqt = types.ModuleType("aqt")
    aqt.mw = _Any()
    aqt.dialogs = _Any()          # DialogManager：addon 載入時就會 register_dialog
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


install_fake_aqt()


# ── log 隔離 ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_llm_log():
    logger = logging.getLogger("whiteforge.llm")
    saved = logger.handlers[:]
    logger.handlers = [logging.NullHandler()]   # 測試期間:log 丟進黑洞
    yield
    logger.handlers = saved                     # 測完還原
