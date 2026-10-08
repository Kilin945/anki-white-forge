"""
My Word Adder — add English words to My Daily English with auto-fill.
Tools > Add English Word… (⌘A / Ctrl+A)  ·  Complete Missing Cards (⌘S / Ctrl+S)
"""

import aqt
from aqt import mw
from aqt.qt import (
    QAction,
)
from ._config import ACTIONS, _shortcut
from ._dlg_add import AddWordDialog, open_dialog
from ._dlg_backfill import BackfillDialog, open_backfill_dialog
from ._dlg_batch import BatchOperationsDialog, open_batch_operations_dialog
from ._dlg_terms import open_translation_terms_dialog
from ._dlg_settings import open_settings_dialog
# 子模組全部明寫：測試用 addon._text.X 這種路徑找名字，不靠上面 from-import 的間接綁定  # noqa: F401
from . import (_config, _text, _llm, _images, _batch, _workers, _dlg_add, _dlg_backfill,
               _sec_translate, _sec_clear_flagged, _sec_duplicates, _sec_long_sentences,
               _sec_test_cards, _dlg_batch, _dlg_terms, _dlg_settings)


# ── menu entries ──────────────────────────────────────────────────────────────

_DIALOGS = (AddWordDialog, BackfillDialog, BatchOperationsDialog)   # 非阻塞批次視窗；新視窗加這裡
_DM_NAMES = {cls: cls._DM_NAME for cls in _DIALOGS}


for _cls, _name in _DM_NAMES.items():
    # 交給 Anki 內建的 dialog manager 管，而不是自己造一個 registry：一次拿到單例、
    # 還原被縮小的視窗、raise、reopen() 重掃，**以及** Anki 退出 / 切 profile 時
    # closeAll() 會來收（自製 registry 它看不到 → worker 會對正在卸載的 collection 續寫）。
    aqt.dialogs.register_dialog(_name, lambda cls=_cls: cls(mw))


def _add_menu_action(title, key, handler):
    act = QAction(title, mw)
    sc = _shortcut(key)
    if sc:
        act.setShortcut(sc)
    act.triggered.connect(handler)
    mw.form.menuTools.addAction(act)
    ACTIONS[key] = act


_add_menu_action("Add English Word…", "add", open_dialog)


_add_menu_action("Complete Missing Cards…", "complete", open_backfill_dialog)


_add_menu_action("Batch Operations…", "backfill_cn", open_batch_operations_dialog)


_add_menu_action("Translation Terms…", "terms", open_translation_terms_dialog)


_settings_action = QAction("Shortcuts…", mw)


_settings_action.triggered.connect(open_settings_dialog)


mw.form.menuTools.addAction(_settings_action)
