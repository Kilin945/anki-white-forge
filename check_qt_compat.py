#!/usr/bin/env python3
"""Anki / Qt 升級後的 addon 相容性檢查。

**為什麼需要這支**：pytest 那批測試用 `conftest.py` 的假 aqt,每個 Qt 類別都被換成
`_Any`(`AddWordDialog.__mro__` 裡根本沒有 `QDialog`)→ 它們驗的是純邏輯,對「這一版
Qt 還能不能用」提供零保證。全綠不代表 GUI 沒事。

這支補的就是那塊:載入 **Anki.app 裡實際在用的那份** PyQt6 與 aqt,用真 Qt 建構四個
對話框、掛真的快捷鍵、走一遍 `aqt.dialogs` 的單例與 `closeAll()`。

不需要 Anki 開著,只需要 Anki.app 存在;offscreen 執行,不會有視窗跳出來。

    uv run python check_qt_compat.py
    ANKI_APP=/path/to/Anki.app uv run python check_qt_compat.py

覆蓋面只到「想得到的破法」為止。之後每遇到一種升級踩雷,就往這裡加一條——
不加的話它會永遠停在今天的覆蓋面,還給人一種跑過了就沒事的錯覺。
"""
import logging
import os
import pathlib
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent
APP = pathlib.Path(os.environ.get("ANKI_APP", "/Applications/Anki.app"))
PKGS = APP / "Contents/Resources/app_packages"

_passed, _failed = 0, []


def check(label):
    """回傳一個 decorator:跑它、記結果、不讓例外中斷整輪。"""
    def deco(fn):
        global _passed
        try:
            detail = fn()
            _passed += 1
            print(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
        except Exception as e:
            _failed.append(label)
            print(f"  ❌ {label} — {type(e).__name__}: {e}")
    return deco


# ── 載入 Anki 自己的 PyQt6 / aqt ────────────────────────────────────────────
# 只放 app_packages:Resources/app 底下有另一個 anki 套件會遮蔽真正的那個。
if not PKGS.is_dir():
    sys.exit(f"找不到 {PKGS}\n設 ANKI_APP 指向 Anki.app,或確認 Anki 已安裝。")
sys.path.insert(0, str(PKGS))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# addon 模組層會 get_logger() 開 RotatingFileHandler 寫 logs/addon_llm.log。
# 先佔住 handler 位置,這輪檢查就不會污染正式 log。
logging.getLogger("whiteforge.llm").addHandler(logging.NullHandler())

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR       # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu, QWidget        # noqa: E402
import aqt                                                      # noqa: E402

_app = QApplication.instance() or QApplication([])


# ── 假 collection / 假 mw ────────────────────────────────────────────────────

class FakeNote(dict):
    def __init__(self, nid, **fields):
        super().__init__(fields)
        self.id = nid


FIELDS = ["Front", "Association", "Sentence", "Sentence_CN",
          "Image_Prompt", "Audio", "Front_Audio", "Translation"]


def _sample_notes():
    """合成卡:一半完整、一半缺欄位,讓各 _scan() 兩條分支都走得到。
    刻意用 13 位數 id(真實 note id 是毫秒時戳)驗 64-bit 傳遞無損。"""
    notes = {}
    for i in range(6):
        nid = 1700000000000 + i
        full = i % 2 == 0
        notes[nid] = FakeNote(
            nid,
            Front=f"word{i}",
            Association="" if i % 3 else "hint",
            Sentence=("A fairly long sentence used for the threshold scan here."
                      if full else ""),
            Sentence_CN="中文翻譯" if full else "",
            Image_Prompt="<img src='x.jpg'>" if full else "",
            Audio="[sound:a.mp3]" if full else "",
            Front_Audio="[sound:f.mp3]" if full else "",
            Translation="翻譯" if full else "",
        )
    return notes


class FakeCol:
    def __init__(self):
        self._notes = _sample_notes()
        self.models = types.SimpleNamespace(by_name=lambda n: {"id": 1})
        self.decks = types.SimpleNamespace(id=lambda n: 1)
        self.media = types.SimpleNamespace(dir=lambda: "/tmp")

    def find_notes(self, query):
        return list(self._notes)

    def find_cards(self, query):
        return []

    def get_note(self, nid):
        return self._notes[nid]

    def get_card(self, cid):
        return types.SimpleNamespace(nid=next(iter(self._notes)))

    def save(self):
        pass

    def set_user_flag_for_cards(self, flag, cids):
        pass

    def update_note(self, note):
        pass


class FakeAddonManager:
    def getConfig(self, _name):
        return None            # None → addon 用 DEFAULT_SHORTCUTS

    def writeConfig(self, _name, _cfg):
        pass


class FakeMw(QWidget):
    """真 QWidget:QAction 要拿它當 parent,假替身在真 Qt 下會被拒。"""
    def __init__(self):
        super().__init__()
        self.form = types.SimpleNamespace(menuTools=QMenu("Tools", self))
        self.addonManager = FakeAddonManager()
        self.col = FakeCol()

    def reset(self):
        pass


def main():
    print(f"Anki.app : {APP}")
    print(f"aqt      : {aqt.appVersion}")
    print(f"PyQt     : {PYQT_VERSION_STR}   Qt: {QT_VERSION_STR}")
    print(f"Python   : {sys.version.split()[0]}")
    print()

    mw = FakeMw()
    aqt.mw = mw                     # addon 做 from aqt import mw,要先塞好
    sys.path.insert(0, str(REPO))

    print("載入")
    @check("import addon（跑真的 register_dialog + 建選單）")
    def _():
        global addon
        import addon as _addon
        addon = _addon
        return f"{len(mw.form.menuTools.actions())} 個選單項"

    if _failed:                     # 載不進來,後面全部沒意義
        return _report()

    print("\nQt 名稱")
    @check("aqt.qt 的 21 個名稱都還在")
    def _():
        import aqt.qt as q
        names = ["QAction", "QDialog", "QVBoxLayout", "QHBoxLayout", "QFormLayout",
                 "QLabel", "QLineEdit", "QPushButton", "QProgressBar", "QScrollArea",
                 "QTreeWidget", "QTreeWidgetItem", "QWidget", "QFrame", "QCheckBox",
                 "QKeySequenceEdit", "QKeySequence", "QMessageBox", "QThread",
                 "pyqtSignal", "Qt"]
        missing = [n for n in names if not hasattr(q, n)]
        assert not missing, f"缺少 {missing}"
        return f"{len(names)} 個"

    @check("addon 的對話框真的繼承 QDialog（不是替身）")
    def _():
        from PyQt6.QtWidgets import QDialog
        assert issubclass(addon.AddWordDialog, QDialog), addon.AddWordDialog.__mro__
        return "pytest 的假 aqt 驗不到這件事"

    print("\n對話框建構（含 __init__ 裡的掃描）")
    for cls_name in ("AddWordDialog", "BackfillDialog",
                     "FindDuplicatesDialog", "BatchOperationsDialog"):
        @check(f"{cls_name} 建構 + 掃描")
        def _(cls_name=cls_name):
            dlg = getattr(addon, cls_name)(mw)
            dlg.close()
            return None

    print("\n快捷鍵")
    @check("四組快捷鍵都綁上且互不重複")
    def _():
        got = {k: a.shortcut().toString() for k, a in addon.ACTIONS.items()}
        assert set(got) == set(addon.DEFAULT_SHORTCUTS), got
        used = [s for s in got.values() if s]
        assert len(used) == len(set(used)), f"重複:{got}"
        return ", ".join(f"{k}={v}" for k, v in sorted(got.items()))

    print("\naqt.dialogs 契約")
    @check("四個視窗都登記進真的 DialogManager")
    def _():
        names = list(addon._DM_NAMES.values())
        missing = [n for n in names if n not in aqt.dialogs._dialogs]
        assert not missing, f"未登記 {missing}"
        return ", ".join(names)

    @check("open() 拿到單例,第二次同一個物件並觸發 reopen()")
    def _():
        name = addon._DM_NAMES[addon.BackfillDialog]
        first = aqt.dialogs.open(name)
        second = aqt.dialogs.open(name)
        assert first is second, "不是單例 → 會同時開兩個批次"
        return type(first).__name__

    @check("closeAll() 收掉全部（Anki 退出 / 切 profile 的路徑）")
    def _():
        done = []
        aqt.dialogs.closeAll(lambda: done.append(True))
        _app.processEvents()
        left = [n for n in addon._DM_NAMES.values()
                if aqt.dialogs._dialogs.get(n, [None, None])[1] is not None]
        assert not left, f"沒收乾淨:{left}"
        return None

    return _report()


def _report():
    print()
    if _failed:
        print(f"❌ {_passed} passed, {len(_failed)} failed")
        for f in _failed:
            print(f"   - {f}")
        return 1
    print(f"✅ {_passed} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
