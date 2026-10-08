"""Tools → Shortcuts 設定視窗（modal）。"""

from aqt import mw
from aqt.qt import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QPushButton, QWidget, QKeySequenceEdit, QKeySequence, Qt,
)
from aqt.utils import showWarning, tooltip
from ._config import ACTIONS, DEFAULT_SHORTCUTS, _shortcut


class SettingsDialog(QDialog):
    """Friendly shortcut editor — press a key combo per action, no JSON, applies live."""

    LABELS = [
        ("add", "Add English Word"),
        ("complete", "Complete Missing Cards"),
        ("backfill_cn", "Batch Operations"),
        ("terms", "Translation Terms"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Shortcuts")
        self.setMinimumWidth(440)
        self._edits = {}
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Click a field and press your key combo; press Clear to unbind (menu only)."))

        form = QFormLayout()
        for key, title in self.LABELS:
            edit = QKeySequenceEdit(QKeySequence(_shortcut(key)))
            edit.setMaximumSequenceLength(1)
            edit.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # only arm when clicked, not on open
            self._edits[key] = edit

            clear = QPushButton("Clear")
            clear.clicked.connect(lambda _, e=edit: e.clear())
            row = QHBoxLayout()
            row.addWidget(edit)
            row.addWidget(clear)
            wrap = QWidget()
            wrap.setLayout(row)
            form.addRow(f"{title}: ", wrap)
        root.addLayout(form)

        btns = QHBoxLayout()
        save = QPushButton("Save")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btns.addWidget(save)
        btns.addWidget(cancel)
        root.addLayout(btns)

        cancel.setFocus()  # start with focus off the key fields — nothing armed

    def _on_save(self):
        new = {key: edit.keySequence().toString() for key, edit in self._edits.items()}
        used = [s for s in new.values() if s]
        if len(used) != len(set(used)):       # same combo on two actions = ambiguous, neither fires
            showWarning("Two actions share the same shortcut; please make them different.")
            return
        cfg = mw.addonManager.getConfig(__package__) or {}
        sc = cfg.setdefault("shortcuts", {})
        sc.update(new)
        mw.addonManager.writeConfig(__package__, cfg)
        for key, act in ACTIONS.items():          # apply live — no restart needed
            act.setShortcut(QKeySequence(sc.get(key, DEFAULT_SHORTCUTS[key])))
        tooltip("Shortcuts updated", period=2000)
        self.accept()


def open_settings_dialog():
    SettingsDialog(mw).exec()
