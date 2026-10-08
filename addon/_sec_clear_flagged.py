"""⌘F 面板第二塊：清空紅旗卡。"""

from aqt import mw
from aqt.qt import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QWidget, Qt,
)
from ._config import DECK_NAME, MODEL_NAME, REFILL_CLEAR_FIELDS
from ._text import _clean_text, _looks_english
from ._images import _image_source, _record_image_reject
from ._batch import _blocked_by_batch, _live_note, _section_title, _sync_after_batch
from ._dlg_backfill import open_backfill_dialog


class ClearFlaggedSection(QWidget):
    """Bottom section of Batch Operations: reset red-flagged cards. Clears every field
    except Word + Association and removes the flag — NO generation (that is Complete
    Missing Cards' job; this section offers a one-click jump). Clearing is synchronous
    and instant, so there is no worker / progress bar / Stop here."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel          # the Batch Operations dialog, so buttons can close it
        self._flagged = []           # [{"nid", "cids", "word"}]
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_section_title("Clear Flagged Cards"))

        desc = QLabel("Clears every field except Word + Association and removes the red "
                      "flag. Regenerate them afterwards with Complete Missing Cards.")
        desc.setWordWrap(True)
        root.addWidget(desc)

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

        # before clearing: a single Clear button (right-aligned). The list above + this
        # press is the only gate — no secondary confirm dialog (matches the old Refill).
        clear_row = QHBoxLayout()
        clear_row.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setEnabled(False)
        self.clear_btn.clicked.connect(self._on_clear)
        clear_row.addWidget(self.clear_btn)
        self.clear_row_w = QWidget()
        self.clear_row_w.setLayout(clear_row)
        root.addWidget(self.clear_row_w)

        # after clearing: optional jump to Complete Missing Cards (left), or just finish (right)
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
        self._flagged = []
        cids = mw.col.find_cards(f'deck:"{DECK_NAME}" note:"{MODEL_NAME}" flag:1')
        by_note = {}
        for cid in cids:
            nid = mw.col.get_card(cid).nid
            by_note.setdefault(nid, []).append(cid)
        words = []
        skipped = 0
        for nid, cardids in by_note.items():
            note = mw.col.get_note(nid)
            word = _clean_text(note["Front"])
            if not _looks_english(word):       # not English → don't touch, leave it flagged
                skipped += 1
                continue
            self._flagged.append({"nid": nid, "cids": cardids, "word": word})
            words.append(word)
        if self._flagged:
            word_text = " · ".join(words)
            if skipped > 0:
                word_text += f"  ({skipped} non-English card(s) skipped)"
            self.word_list.setText(word_text)
            self.clear_btn.setText(f"Clear {len(self._flagged)} Cards")
            self.clear_btn.setEnabled(True)
        elif skipped > 0:
            self.word_list.setText(f"No English flagged cards ({skipped} skipped — not English).")
            self.clear_btn.setEnabled(False)
        else:
            self.word_list.setText("No flagged cards.")
            self.clear_btn.setEnabled(False)

    def _on_clear(self):
        if not self._flagged:
            return
        if _blocked_by_batch(self.status.setText):
            self.status.setVisible(True)
            return
        n = 0
        for item in self._flagged:
            note = _live_note(item["nid"])       # 視窗非阻塞後卡片可能已被別處刪掉
            if note is None:
                continue
            # 紅旗 = 使用者說這張圖不行 → 清掉前先記成退圖，下次 ⌘S 搜圖會換圖源／換一張
            _record_image_reject(item["word"], _image_source(note["Image_Prompt"]))
            for f in REFILL_CLEAR_FIELDS:        # keep Front + Association, blank the rest
                if f in note:
                    note[f] = ""
            mw.col.update_note(note)
            mw.col.set_user_flag_for_cards(0, item["cids"])
            n += 1
        mw.col.save()
        mw.reset()
        _sync_after_batch()
        self.word_list.setText("")
        self.clear_row_w.setVisible(False)
        self.status.setText(f"✓ Cleared {n} card(s) and removed their flags. "
                            "Word + Association kept. Regenerate them now?")
        self.status.setVisible(True)
        self.post_row_w.setVisible(True)

    def _open_complete(self):
        self._panel.accept()         # close the panel, then jump to Complete Missing Cards
        open_backfill_dialog()

    def _done(self):
        self._panel.accept()
