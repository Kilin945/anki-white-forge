"""⌘F Batch Operations 面板本體：把五個 section 疊起來。"""

from aqt.qt import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea, QWidget, QFrame,
)
from ._batch import _BatchDialogMixin, _hline, _show_nonmodal
from ._sec_translate import TranslateSection
from ._sec_clear_flagged import ClearFlaggedSection
from ._sec_duplicates import DuplicatesSection
from ._sec_long_sentences import LongSentencesSection
from ._sec_test_cards import TestCardsSection


class BatchOperationsDialog(_BatchDialogMixin, QDialog):
    """Unified batch panel, top to bottom: sentence-translation backfill,
    clear-flagged, find duplicates, rebuild long sentences, test-card helper,
    separated by dividers. Built from stacked self-contained
    section widgets so more batch operations can be added as new blocks."""

    _DM_NAME = "WhiteForgeBatchOps"
    _BATCH_LABEL = "Batch Operations"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Batch Operations")
        self.setMinimumSize(660, 760)      # 五個 section 疊起來已超過小視窗 → 給足高度

        # sections 放進可捲動的內容區:每個 section 保有自然高度、不再互相擠壓
        # (曾經四塊硬塞固定視窗 → 說明文字被裁、按鈕疊到清單上)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)   # 右緣留給捲軸
        self._translate = TranslateSection(self, parent=content)
        clear_flagged  = ClearFlaggedSection(self, parent=content)
        duplicates     = DuplicatesSection(self, parent=content)
        long_sentences = LongSentencesSection(self, parent=content)
        test_cards     = TestCardsSection(self, parent=content)
        self._sections = [self._translate, clear_flagged, duplicates,
                          long_sentences, test_cards]
        body.addWidget(self._translate)
        body.addWidget(_hline())
        body.addWidget(clear_flagged)
        body.addWidget(_hline())
        body.addWidget(duplicates)
        body.addWidget(_hline())
        body.addWidget(long_sentences)
        body.addWidget(_hline())
        body.addWidget(test_cards)
        body.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        root = QVBoxLayout(self)
        root.addWidget(scroll)
        root.addWidget(_hline())

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)         # Close 固定在捲動區外,永遠可見

    # 這個面板的 worker 掛在 TranslateSection 上,不在面板自己身上 → 覆寫這兩個 hook
    def _active_worker(self):
        w = getattr(self._translate, "_worker", None)
        return w if (w is not None and w.isRunning()) else None

    def _set_batch_status(self, text):
        self._translate.status.setText(text)

    def reopen(self):
        """單例被叫回前面 → 各 section 重掃，否則計數與清單是上次開窗時的。
        翻譯批次還在跑就不動它。"""
        for section in self._sections:
            if section is self._translate and self._batch_active():
                continue
            scan = getattr(section, "_scan", None)
            if scan is not None:
                scan()


def open_batch_operations_dialog():
    _show_nonmodal(BatchOperationsDialog)
