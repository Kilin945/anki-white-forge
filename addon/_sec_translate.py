"""⌘F 面板第一塊：批次補整句翻譯。"""

from aqt import mw
from aqt.qt import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar, QWidget, Qt,
)
from ._config import PLACEHOLDERS
from ._text import _clean_text
from ._batch import _batch_acquire, _batch_busy_message, _deck_note_ids, _section_title, _sync_after_batch
from ._workers import SentenceCNWorker


# ── 批次回填整句翻譯（Sentence_CN）—— burst 引擎 + 時間盒選單 ───────────────────

SENTENCE_CN_RPM = 25          # 約略每分鐘筆數（Groq 12000 token/分 ÷ ~480/句 ≈ 25）；僅用於預估顯示


# (label, budget_seconds | None=直接完成)
SENTENCE_CN_MODES = [("1 min", 60), ("2 min", 120), ("5 min", 300),
                     ("10 min", 600), ("Run to completion", None)]


class TranslateSection(QWidget):
    """Top section of Batch Operations: bulk-fill Sentence_CN with an up-front time
    estimate and a time-box menu. Paced by SentenceCNWorker; resume is automatic
    (each scan re-checks what's missing)."""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel          # the Batch Operations dialog (批次互斥 + 關窗收尾)
        self._worker = None
        self._notes = []
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_section_title("Backfill Sentence Translations"))

        self.info = QLabel()
        self.info.setWordWrap(True)
        root.addWidget(self.info)

        note = QLabel(
            f"Groq translates only about {SENTENCE_CN_RPM} per minute; a longer time just extends the run, waiting for quota to refill and continuing.\n"
            "You can press Stop any time; reopening resumes from what's left.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#64748b; font-size:12px;")
        root.addWidget(note)

        self._mode_row = QHBoxLayout()
        self._mode_btns = []
        for label, secs in SENTENCE_CN_MODES:
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, s=secs: self._start(s))
            self._mode_row.addWidget(b)
            self._mode_btns.append((b, secs))
        root.addLayout(self._mode_row)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        stop_row = QHBoxLayout()
        stop_row.addStretch()
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        stop_row.addWidget(self.stop_btn)
        root.addLayout(stop_row)

    def _scan(self):
        notes = []
        for nid in _deck_note_ids():
            n = mw.col.get_note(nid)
            if "Sentence_CN" not in n or "Sentence" not in n:
                continue
            sentence = _clean_text(n["Sentence"])
            if not sentence or any(p in n["Sentence"] for p in PLACEHOLDERS):
                continue                      # no real sentence to translate yet
            if n["Sentence_CN"].strip():
                continue                      # already has a translation
            notes.append({"noteId": nid, "sentence": sentence,
                          "word": _clean_text(n["Front"], lower=True)})
        self._notes = notes
        n = len(notes)
        if n == 0:
            self.info.setText("All cards already have sentence translations.")
            for b, _secs in self._mode_btns:
                b.setEnabled(False)
        else:
            est = -(-n // SENTENCE_CN_RPM)     # ceil(n / rpm) minutes
            self.info.setText(f"{n} card(s) missing a sentence translation, ~{SENTENCE_CN_RPM}/min → about {est} min total.")
            for b, _secs in self._mode_btns:
                b.setEnabled(True)

    def _start(self, budget_seconds):
        if not self._notes:
            return
        if not _batch_acquire(self._panel._BATCH_LABEL):   # ⌘S 也寫 Sentence_CN → 不准同時跑
            self.status.setText(_batch_busy_message())
            return
        for b, _secs in self._mode_btns:
            b.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(self._notes))
        self.progress_bar.setValue(0)
        self._worker = SentenceCNWorker(self._notes, budget_seconds)
        self._worker.progress.connect(self._on_progress)
        self._worker.waiting.connect(self._on_waiting)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_progress(self, done, total, remaining_secs):
        self.progress_bar.setValue(done)
        tail = "" if remaining_secs < 0 else f"({remaining_secs}s left)"
        self.status.setText(f"Translating… {done} / {total} {tail}")

    def _on_waiting(self, secs, done, total):
        self.status.setText(f"Waiting for quota… auto-resume in {secs}s (translated {done} / {total})")

    def _on_stop(self):
        if self._worker:
            self._worker.stop()
        self.stop_btn.setEnabled(False)
        self.status.setText("Stopping…")

    def _on_finished(self, done, remaining):
        mw.col.save()
        mw.reset()
        _sync_after_batch()
        self.progress_bar.setVisible(False)
        self.stop_btn.setEnabled(False)
        blocked = getattr(self._worker, "blocked_secs", 0)
        if blocked:
            self.status.setText(
                f"Translated {done}. Hit Groq's longer rate limit (need to wait ~{blocked}s, "
                f"possibly the daily quota); please come back later. {remaining} left.")
        else:
            self.status.setText(f"Translated {done} this run, {remaining} left.")
        self._scan()       # refresh count + re-enable mode buttons for another round
        self._panel._end_batch()
