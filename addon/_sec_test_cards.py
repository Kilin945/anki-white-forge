"""⌘F 面板第五塊：產生／清除測試卡。KEEP-IN-SYNC 對照 tools/make_test_cards.py。"""

from aqt import mw
from aqt.qt import (
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget,
)
from ._config import DECK_NAME, MODEL_NAME
from ._batch import _blocked_by_batch, _section_title
from ._dlg_backfill import open_backfill_dialog


# Test-card helper — bare cards for manually testing the dialogs. KEEP IN SYNC with
# tools/make_test_cards.py (CLI): same tag + same word list, so a card made by one tool is
# cleaned by the other. (addon cannot import the CLI/core module.)
TEST_CARD_TAG = "whiteforge_test"


TEST_CARD_WORDS = [
    ("zztestalpha",   "a test word"),
    ("zztestbravo",   "a test word"),
    ("zztestcharlie", "a test word"),
    ("zztestdelta",   "a test word"),
    ("zztestecho",    "a test word"),
    ("zztestfoxtrot", "a test word"),
    ("zztestgolf",    "a test word"),
    ("zztesthotel",   "a test word"),
    ("zztestindia",   "a test word"),
    ("zztestjuliet",  "a test word"),
]


def _clamp_test_count(raw, default=7):
    """Parse the Count field → int in [1, len(TEST_CARD_WORDS)]. Blank/non-numeric →
    default; out of range → clamped. Pure decision behind Add Test Cards."""
    try:
        n = int(str(raw).strip())
    except (ValueError, TypeError):
        return default
    return max(1, min(n, len(TEST_CARD_WORDS)))


class TestCardsSection(QWidget):
    """Batch Operations section: spawn / clean throwaway test cards (Front + Association
    only) so they show up in Complete Missing Cards for manual UI testing. Cards carry
    TEST_CARD_TAG so cleanup is one click. Synchronous — no worker/progress bar.
    UI-side twin of tools/make_test_cards.py (CLI)."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel          # the Batch Operations dialog, so buttons can close it
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_section_title("Test Cards"))

        desc = QLabel("Create bare test cards (Front + Association only) so they show up "
                      "in Complete Missing Cards for testing. Clean removes them by tag.")
        desc.setWordWrap(True)
        root.addWidget(desc)

        row = QHBoxLayout()
        row.addWidget(QLabel("Count:"))
        self.count_input = QLineEdit("7")
        self.count_input.setFixedWidth(50)
        row.addWidget(self.count_input)
        self.add_btn = QPushButton("Add Test Cards")
        self.add_btn.clicked.connect(self._on_add)
        row.addWidget(self.add_btn)
        self.clean_btn = QPushButton("Clean Test Cards")
        self.clean_btn.clicked.connect(self._on_clean)
        row.addWidget(self.clean_btn)
        row.addStretch()
        row_w = QWidget()
        row_w.setLayout(row)
        root.addWidget(row_w)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color:#16a34a; font-weight:600;")
        self.status.setVisible(False)
        root.addWidget(self.status)

        # after adding: one-click jump to Complete Missing Cards to test them
        post_row = QHBoxLayout()
        self.open_complete_btn = QPushButton("Open Complete Missing Cards")
        self.open_complete_btn.clicked.connect(self._open_complete)
        post_row.addWidget(self.open_complete_btn)
        post_row.addStretch()
        self.post_row_w = QWidget()
        self.post_row_w.setLayout(post_row)
        self.post_row_w.setVisible(False)
        root.addWidget(self.post_row_w)

    def _set_status(self, text):
        self.status.setText(text)
        self.status.setVisible(True)

    def _on_add(self):
        if _blocked_by_batch(self._set_status):
            return
        n = _clamp_test_count(self.count_input.text())
        model = mw.col.models.by_name(MODEL_NAME)
        if not model:
            self._set_status(f"Note type '{MODEL_NAME}' not found.")
            return
        deck_id = mw.col.decks.id(DECK_NAME)
        added = 0
        for word, assoc in TEST_CARD_WORDS[:n]:
            if mw.col.find_notes(f'deck:"{DECK_NAME}" Front:"{word}"'):
                continue                      # already there → skip, no duplicates
            note = mw.col.new_note(model)
            note["Front"] = word
            note["Association"] = assoc
            note.tags.append(TEST_CARD_TAG)
            mw.col.add_note(note, deck_id)
            added += 1
        mw.col.save()
        mw.reset()
        self._set_status(f"✓ Added {added} test card(s). Open Complete Missing Cards to test.")
        self.post_row_w.setVisible(True)

    def _on_clean(self):
        if _blocked_by_batch(self._set_status):
            return
        nids = mw.col.find_notes(f"tag:{TEST_CARD_TAG}")
        if not nids:
            self._set_status("No test cards to clean.")
            self.post_row_w.setVisible(False)
            return
        mw.col.remove_notes(nids)
        mw.col.save()
        mw.reset()
        self._set_status(f"✓ Deleted {len(nids)} test card(s).")
        self.post_row_w.setVisible(False)

    def _open_complete(self):
        self._panel.accept()         # close the panel, then jump to Complete Missing Cards
        open_backfill_dialog()
