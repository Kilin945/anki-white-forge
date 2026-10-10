"""⌘D Translation Terms 視窗。"""

import html
from aqt import mw
from aqt.qt import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QWidget,
)
from aqt.utils import showWarning
from . import _config
from ._text import _TERMS_LOCK, load_translation_terms, save_translation_terms
from ._batch import _hline, _selectable_all


class TranslationTermsDialog(QDialog):
    """⌘D：維護整句翻譯的術語白名單。Pending = 被驗證丟掉的英文片語，Approve 進白名單、
    Discard 丟掉；也能手動 Add、在 Manage 面板 Remove。所有動作立刻存檔，沒有草稿狀態。
    是設定視窗不是批次視窗 → modal exec()，不走 _show_nonmodal。不 setStyleSheet（CLAUDE.md 的坑）。"""

    LIST_MIN_HEIGHT = 260
    LIST_MAX_HEIGHT = 360

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Translation Terms")
        self.setMinimumWidth(640)
        self.resize(700, 480)
        self._data = load_translation_terms(_config.TRANSLATION_TERMS_PATH)
        self._setup_ui()
        self._render_pending()
        self._refresh_terms()
        _selectable_all(self)            # 文字都能用滑鼠選取複製

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Terms dropped by the translation check. Approve the real ones."))

        self._pending_box = QVBoxLayout()
        pending_wrap = QWidget()
        pending_wrap.setLayout(self._pending_box)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(pending_wrap)
        scroll.setMinimumHeight(200)
        root.addWidget(scroll, 1)

        root.addWidget(QLabel("Add a term:"))
        add_row = QHBoxLayout()
        self.add_edit = QLineEdit()
        self.add_edit.setPlaceholderText("e.g. race condition")
        self.add_edit.returnPressed.connect(self._on_add)
        self.add_btn = QPushButton("Add")
        self.add_btn.clicked.connect(self._on_add)
        add_row.addWidget(self.add_edit, 1)
        add_row.addWidget(self.add_btn)
        root.addLayout(add_row)

        count_row = QHBoxLayout()
        self.count_label = QLabel("")
        self.manage_btn = QPushButton("Manage…")
        self.manage_btn.clicked.connect(self._toggle_manage)
        count_row.addWidget(self.count_label)
        count_row.addStretch()
        count_row.addWidget(self.manage_btn)
        root.addLayout(count_row)

        # 展開面板:預設收起
        self.panel = QWidget()
        panel_box = QVBoxLayout(self.panel)
        panel_box.setContentsMargins(0, 0, 0, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search")
        self.search_edit.textChanged.connect(lambda _=None: self._render_terms())
        panel_box.addWidget(self.search_edit)
        self._terms_box = QVBoxLayout()
        terms_wrap = QWidget()
        terms_wrap.setLayout(self._terms_box)
        self._terms_scroll = QScrollArea()
        self._terms_scroll.setWidgetResizable(True)
        self._terms_scroll.setWidget(terms_wrap)
        # 清單要有最小高度,否則展開時視窗不長高、清單被壓到只剩一列(2026-10-03 實測)
        self._terms_scroll.setMinimumHeight(self.LIST_MIN_HEIGHT)
        self._terms_scroll.setMaximumHeight(self.LIST_MAX_HEIGHT)
        panel_box.addWidget(self._terms_scroll)
        self.panel.setVisible(False)
        root.addWidget(self.panel)

        btns = QHBoxLayout()
        btns.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.reject)
        btns.addWidget(self.close_btn)
        root.addLayout(btns)

    @staticmethod
    def _clear_layout(box):
        while box.count():
            item = box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()                       # 不 setParent(None):按下的按鈕就在這列裡
                w.deleteLater()

    def _render_pending(self):
        """依 self._data["pending"] 重畫 Pending 區。"""
        self._clear_layout(self._pending_box)
        self.approve_buttons, self.discard_buttons = {}, {}
        if not self._data["pending"]:
            self._pending_box.addWidget(QLabel("No pending terms."))
            _selectable_all(self)
            return
        for p in self._data["pending"]:
            term = p["term"]
            col = QVBoxLayout()
            col.setContentsMargins(6, 4, 6, 4)
            col.setSpacing(2)
            col.addWidget(QLabel(f"<b>{html.escape(term)}</b>"))
            col.addWidget(QLabel(f"from: {html.escape(p.get('word', ''))}"))
            tr = QLabel(f"「{html.escape(p.get('translation', ''))}」")
            tr.setWordWrap(True)
            col.addWidget(tr)
            approve = QPushButton("Approve")
            approve.clicked.connect(lambda _=False, t=term: self._on_approve(t))
            discard = QPushButton("Discard")
            discard.clicked.connect(lambda _=False, t=term: self._on_discard(t))
            btn_row = QHBoxLayout()
            btn_row.addStretch()
            btn_row.addWidget(approve)
            btn_row.addWidget(discard)
            col.addLayout(btn_row)
            wrap = QWidget()
            wrap.setLayout(col)
            self._pending_box.addWidget(_hline())
            self._pending_box.addWidget(wrap)
            self.approve_buttons[term] = approve
            self.discard_buttons[term] = discard
        _selectable_all(self)

    def _refresh_terms(self):
        self.count_label.setText(f"Approved terms: {len(self._data['terms'])}")
        if self.panel.isVisible():
            self._render_terms()

    def _render_terms(self):
        """依搜尋框過濾（不分大小寫子字串）、按字母排序重畫清單。"""
        self._clear_layout(self._terms_box)
        self.remove_buttons = {}
        q = self.search_edit.text().strip().lower()
        for term in sorted(t for t in self._data["terms"] if q in t.lower()):
            row = QHBoxLayout()
            row.setContentsMargins(6, 2, 6, 2)
            row.addWidget(QLabel(html.escape(term)), 1)
            rm = QPushButton("Remove")
            rm.clicked.connect(lambda _=False, t=term: self._on_remove(t))
            row.addWidget(rm)
            wrap = QWidget()
            wrap.setLayout(row)
            self._terms_box.addWidget(wrap)
            self._terms_box.addWidget(_hline())
            self.remove_buttons[term] = rm
        self._terms_box.addStretch()
        _selectable_all(self)

    def _toggle_manage(self):
        show = not self.panel.isVisible()
        self.panel.setVisible(show)
        self.manage_btn.setText("Hide" if show else "Manage…")
        if show:
            self._render_terms()
        # 展開要長高、收起要縮回:QDialog 不會自己跟著 layout 變
        self.layout().activate()
        self.resize(self.width(), self.sizeHint().height())

    def _mutate(self, change):
        """重讀檔案 → 套用 change(data) → 存檔，整段在鎖內（⌘S worker 可能同時在記新的待審）。
        存檔失敗跳警告、不動畫面並回 False。"""
        with _TERMS_LOCK:
            data = load_translation_terms(_config.TRANSLATION_TERMS_PATH)
            change(data)
            if not save_translation_terms(data, _config.TRANSLATION_TERMS_PATH):
                showWarning("Could not write translation_terms.json.")
                return False
            self._data = load_translation_terms(_config.TRANSLATION_TERMS_PATH)
        return True

    def _on_approve(self, term):
        def change(d):
            d["pending"] = [p for p in d["pending"] if p["term"] != term]
            if term not in d["terms"]:
                d["terms"].append(term)
        if self._mutate(change):
            self._render_pending()
            self._refresh_terms()

    def _on_discard(self, term):
        def change(d):
            d["pending"] = [p for p in d["pending"] if p["term"] != term]
        if self._mutate(change):
            self._render_pending()

    def _on_add(self):
        term = " ".join(self.add_edit.text().lower().split())
        if not term:
            return
        def change(d):
            if term not in d["terms"]:
                d["terms"].append(term)
        if self._mutate(change):
            self.add_edit.clear()
            self._refresh_terms()

    def _on_remove(self, term):
        def change(d):
            d["terms"] = [t for t in d["terms"] if t != term]
        if self._mutate(change):
            self._refresh_terms()


def open_translation_terms_dialog():
    TranslationTermsDialog(mw).exec()
