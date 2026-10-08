"""⌘F 面板第四塊：清空過長例句。"""

from aqt import mw
from aqt.qt import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QWidget, Qt,
)
from ._config import REBUILD_CLEAR_FIELDS
from ._text import _clamp_length_threshold, _clean_text, _long_sentence_label, _looks_english, _sentence_word_count
from ._batch import _blocked_by_batch, _deck_note_ids, _live_note, _section_title, _sync_after_batch
from ._dlg_backfill import open_backfill_dialog


class LongSentencesSection(QWidget):
    """Batch Operations section: find sentences longer than a threshold and clear
    REBUILD_CLEAR_FIELDS (everything except Front / Association / Front_Audio —
    換句=全重建) so the existing pipelines regenerate short ones — clearing only,
    NO generation here (that is Complete Missing Cards' / the CLI's job).
    Synchronous, no worker.
    背景:句長規則(6-12字)是後來才進 prompt 的,舊卡留下大量長句(實測 >20 字 55 張)。"""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel          # the Batch Operations dialog, so buttons can close it
        self._hits = []              # [{"nid", "word", "count"}] 字數降冪
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_section_title("Rebuild Long Sentences"))

        desc = QLabel("Old cards predate the 6-12 word sentence rule. For sentences longer "
                      "than the threshold, clears everything except the word, its hint and "
                      "its pronunciation — regenerate with Complete Missing Cards.")
        desc.setWordWrap(True)
        root.addWidget(desc)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Longer than:"))
        self.threshold_input = QLineEdit("20")
        self.threshold_input.setFixedWidth(50)
        ctl.addWidget(self.threshold_input)
        ctl.addWidget(QLabel("words"))
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self._scan)
        ctl.addWidget(rescan)
        ctl.addStretch()
        ctl_w = QWidget()
        ctl_w.setLayout(ctl)
        root.addWidget(ctl_w)

        self.word_list = QLabel("")
        self.word_list.setWordWrap(True)
        self.word_list.setStyleSheet("color:#475569; padding:4px;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.word_list)
        scroll.setMinimumHeight(70)
        root.addWidget(scroll)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet("color:#16a34a; font-weight:600;")
        self.status.setVisible(False)
        root.addWidget(self.status)

        # before clearing: single Clear button (right-aligned); the list above + this
        # press is the only gate — no secondary confirm dialog (matches ClearFlagged).
        clear_row = QHBoxLayout()
        clear_row.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setEnabled(False)
        self.clear_btn.clicked.connect(self._on_clear)
        clear_row.addWidget(self.clear_btn)
        self.clear_row_w = QWidget()
        self.clear_row_w.setLayout(clear_row)
        root.addWidget(self.clear_row_w)

        # after clearing: optional jump to Complete Missing Cards (left), or just finish
        post_row = QHBoxLayout()
        self.open_complete_btn = QPushButton("Open Complete Missing Cards")
        self.open_complete_btn.clicked.connect(self._open_complete)
        post_row.addWidget(self.open_complete_btn)
        post_row.addStretch()
        self.done_btn = QPushButton("Done")
        self.done_btn.clicked.connect(self._done)
        post_row.addWidget(self.done_btn)
        self.post_row_w = QWidget()
        self.post_row_w.setLayout(post_row)
        self.post_row_w.setVisible(False)
        root.addWidget(self.post_row_w)

    def _scan(self):
        threshold = _clamp_length_threshold(self.threshold_input.text())
        self.threshold_input.setText(str(threshold))   # 正規化顯示(空白/非數字→預設)
        self._hits = []
        for nid in _deck_note_ids():
            note = mw.col.get_note(nid)
            word = _clean_text(note["Front"])
            if not _looks_english(word):       # 非英文 → 不碰
                continue
            count = _sentence_word_count(note["Sentence"])
            if count > threshold:
                self._hits.append({"nid": nid, "word": word, "count": count})
        self._hits.sort(key=lambda h: h["count"], reverse=True)
        self.status.setVisible(False)
        self.clear_row_w.setVisible(True)
        self.post_row_w.setVisible(False)
        if self._hits:
            self.word_list.setText(" · ".join(
                _long_sentence_label(h["word"], h["count"]) for h in self._hits))
            self.clear_btn.setText(f"Clear {len(self._hits)} Sentences")
            self.clear_btn.setEnabled(True)
        else:
            self.word_list.setText(f"No sentences longer than {threshold} words.")
            self.clear_btn.setText("Clear")
            self.clear_btn.setEnabled(False)

    def _on_clear(self):
        if not self._hits:
            return
        if _blocked_by_batch(self.status.setText):
            self.status.setVisible(True)
            return
        n = 0
        for h in self._hits:
            note = _live_note(h["nid"])          # 視窗非阻塞後卡片可能已被別處刪掉
            if note is None:
                continue
            for f in REBUILD_CLEAR_FIELDS:       # 換句=全重建(留 Front/Association/Front_Audio)
                if f in note:
                    note[f] = ""
            mw.col.update_note(note)
            n += 1
        mw.col.save()
        mw.reset()
        _sync_after_batch()
        self._hits = []
        self.word_list.setText("")
        self.clear_row_w.setVisible(False)
        self.status.setText(f"✓ Cleared {n} card(s) — sentence, translations, image "
                            "and audio. Regenerate them now?")
        self.status.setVisible(True)
        self.post_row_w.setVisible(True)

    def _open_complete(self):
        self._panel.accept()         # close the panel, then jump to Complete Missing Cards
        open_backfill_dialog()

    def _done(self):
        self._panel.accept()
