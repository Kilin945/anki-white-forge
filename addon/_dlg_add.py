"""⌘A Add English Word 視窗。"""

import json
import subprocess
from aqt import mw
from aqt.qt import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QLineEdit, QPushButton, QProgressBar, QMessageBox, QTimer, Qt,
)
from aqt.utils import showWarning, tooltip
from . import _llm
from ._config import BOX_STYLE, DECK_NAME, FIELD_BOXES, MODEL_NAME, VALIDATE_SCRIPT, VENV_PYTHON, _FIELD_LABEL
from ._text import _clean_text, _looks_english, _reasons_text
from ._batch import _BatchDialogMixin, _batch_acquire, _batch_busy_message, _deck_note_ids, _show_nonmodal, _sync_after_batch, _selectable_all, LiveActivity
from ._workers import Worker


# ── dialog ───────────────────────────────────────────────────────────────────

class AddWordDialog(_BatchDialogMixin, QDialog):
    _STATUS_STYLE = {
        "info": "font-size:13px; color:#64748b;",
        "ok":   "font-size:18px; color:#16a34a; font-weight:700; padding:6px;",
        "warn": "font-size:14px; color:#ea580c; font-weight:600;",
    }

    _DM_NAME = "WhiteForgeAddWord"
    _BATCH_LABEL = "Add English Word"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add English Word")
        self.setMinimumWidth(580)        # wider than the 5-box row so the stretches centre it (side margins)
        self._worker = None
        self._setup_ui()
        _selectable_all(self)            # 文字都能用滑鼠選取複製

    def _set_batch_status(self, text):
        self._set_status(text, "info")

    def _setup_ui(self):
        root = QVBoxLayout(self)

        form = QFormLayout()
        self.word_input  = QLineEdit()
        self.word_input.setPlaceholderText("e.g. ephemeral")
        self.assoc_input = QLineEdit()
        self.assoc_input.setPlaceholderText("e.g. fleeting, transient  (optional)")
        form.addRow("Word:", self.word_input)
        form.addRow("Association:", self.assoc_input)
        root.addLayout(form)

        # per-field progress boxes — shown when adding, each flips to ✓ when done
        self._boxes = {}
        self._reasons = {}                # field key → 退回原因短句（完整原因在 log）
        boxes_row = QHBoxLayout()
        boxes_row.setSpacing(8)           # gap between boxes
        boxes_row.addStretch()            # stretches centre the fixed-width group (no word col here)
        for key, label in FIELD_BOXES:
            box = QLabel(label)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.setFixedWidth(90)         # uniform box width regardless of label length
            box.setVisible(False)
            self._boxes[key] = box
            boxes_row.addWidget(box)
        boxes_row.addStretch()
        root.addLayout(boxes_row)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)    # 錯誤訊息可能多行/長 → 換行不截斷
        root.addWidget(self.status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        btns = QHBoxLayout()
        self.add_btn = QPushButton("Add Card")
        self.add_btn.setDefault(True)
        self.add_btn.clicked.connect(self._on_add)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(self.add_btn)
        btns.addWidget(close_btn)
        root.addLayout(btns)
        # NOTE: don't also wire word_input.returnPressed → _on_add. add_btn is the
        # dialog's default button, so Enter already triggers it; connecting returnPressed
        # as well fires _on_add twice → the spell-check/confirm dialog pops up twice.

    def _set_box(self, key, state, reason=""):
        box = self._boxes.get(key)
        if not box:
            return
        style, fmt = BOX_STYLE[state]
        box.setStyleSheet(style)
        box.setText(fmt.format(_FIELD_LABEL[key]))
        if state == "warn" and reason:
            self._reasons[key] = reason
            box.setToolTip(reason)
        else:
            self._reasons.pop(key, None)
            box.setToolTip("")

    def _start_boxes(self):
        self._reasons = {}
        for key in self._boxes:
            self._boxes[key].setVisible(True)
            self._set_box(key, "working")

    def _set_status(self, text, kind="info"):
        self.status.setStyleSheet(self._STATUS_STYLE[kind])
        self.status.setText(text)

    def _spellcheck(self, word):
        """(status, suggestion) — Groq primary, offline pyspellchecker fallback."""
        status, suggestion = _llm._groq_spellcheck(word)
        if status != "unknown":
            return status, suggestion
        if " " in word:          # offline speller treats a phrase as one token → false typo; skip
            return "ok", None
        try:
            result = subprocess.run(
                [VENV_PYTHON, VALIDATE_SCRIPT, "word", word],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout.strip())
                if data.get("valid"):
                    return "ok", None
                sugg = data.get("suggestions", [])
                if sugg:
                    return "typo", sugg[0]
        except Exception:
            pass
        return "unknown", None

    def _validate_word_ui(self, word):
        """Returns the word to add (possibly corrected), or None to abort."""
        # Layer 1 — charset hard block: any non-English letter / digit is definitely wrong
        if not _looks_english(word):
            showWarning(f"'{word}' contains non-English characters and cannot be added.")
            return None

        # Layer 2 — spelling (Groq, offline fallback)
        status, suggestion = self._spellcheck(word)
        if status == "ok":
            return word

        if status == "typo" and suggestion and suggestion != word:
            box = QMessageBox(self)
            box.setWindowTitle("Spell Check")
            box.setText(f"'{word}' may be misspelled. Did you mean '{suggestion}'?")
            use_btn  = box.addButton(f"Use '{suggestion}'", QMessageBox.ButtonRole.AcceptRole)
            keep_btn = box.addButton(f"Keep '{word}'", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(use_btn)
            box.exec()
            clicked = box.clickedButton()
            if clicked is use_btn:
                return suggestion
            if clicked is keep_btn:
                return word
            return None

        # unknown / no usable suggestion → let the user decide
        reply = QMessageBox.question(
            self, "Word Not Found",
            f"'{word}' was not found and may be misspelled. Add it anyway?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return word if reply == QMessageBox.StandardButton.Yes else None

    def _validate_assoc_ui(self, assoc):
        """Returns (possibly unchanged) assoc, or None if user cancelled."""
        if not assoc:
            return assoc
        try:
            result = subprocess.run(
                [VENV_PYTHON, VALIDATE_SCRIPT, "assoc", assoc],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                issues = json.loads(result.stdout.strip()).get("issues", [])
                if issues:
                    lines = []
                    for i in issues:
                        hint = f" → maybe: {', '.join(i['suggestions'])}" if i["suggestions"] else ""
                        lines.append(f"  '{i['word']}'{hint}")
                    reply = QMessageBox.warning(
                        self, "Possible Typos in Association",
                        "Possible typos detected:\n" + "\n".join(lines) + "\n\nContinue?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    if reply != QMessageBox.StandardButton.Yes:
                        return None
        except Exception:
            pass
        return assoc

    def _on_add(self):
        word = self.word_input.text().strip().lower()
        if not word:
            showWarning("Please enter a word.")
            return

        self._set_status("Checking spelling…")
        word = self._validate_word_ui(word)
        if word is None:
            self.status.setText("")
            return
        self.word_input.setText(word)

        assoc = self._validate_assoc_ui(self.assoc_input.text().strip())
        if assoc is None:
            self.status.setText("")
            return

        # duplicate check (normalized: catches HTML / case / whitespace variants,
        # not just exact match — e.g. an existing "<div>audit</div>" or "Audit")
        target = _clean_text(word, lower=True)
        if any(_clean_text(mw.col.get_note(nid)["Front"], lower=True) == target
               for nid in _deck_note_ids()):
            self._set_status(f"'{word}' already exists in the deck.", "warn")
            return

        if not _batch_acquire(self._BATCH_LABEL):     # 別的批次在跑 → 會互搶額度
            self._set_status(_batch_busy_message(), "warn")
            return

        self.add_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._set_status(f"Generating: {word}")
        self._start_boxes()

        # 即時進度：現在在做哪一步、用哪個模型、過了幾秒（找圖時再加倒數），每秒刷新
        self._live = LiveActivity()
        self._live_word = word
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._show_live)
        self._live_timer.start(1000)

        self._worker = Worker(word, assoc, mw.col.media.dir())
        self._worker.step.connect(self._set_box)
        self._worker.activity.connect(self._on_activity)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_activity(self, text, countdown):
        self._live.update(text, countdown)
        self._show_live()

    def _show_live(self):
        line = self._live.line()
        if line:
            self._set_status(f"Generating '{self._live_word}' — {line}")

    def _stop_live(self):
        timer = getattr(self, "_live_timer", None)
        if timer is not None:
            timer.stop()

    def _on_finished(self, data):
        self._stop_live()
        try:
            model = mw.col.models.by_name(MODEL_NAME)
            if not model:
                raise RuntimeError(f"Note type '{MODEL_NAME}' not found.")

            note = mw.col.new_note(model)
            note["Front"]       = data["word"]
            note["Association"] = data["association"]
            note["Sentence"]    = data["sentence"]
            note["Image_Prompt"] = data["image_field"]
            note["Audio"]       = f'[sound:{data["audio_filename"]}]' if data["audio_filename"] else ""
            note["Front_Audio"] = f'[sound:{data["front_audio_filename"]}]'
            if "Translation" in note:
                note["Translation"] = data.get("translation", "")
            if "Sentence_CN" in note:
                note["Sentence_CN"] = data.get("sentence_cn", "")

            deck_id = mw.col.decks.id(DECK_NAME)
            mw.col.add_note(note, deck_id)
            mw.col.save()
            mw.reset()
            _sync_after_batch()

            if self._reasons:             # 有欄位被退：狀態列用短句講原因（細節在 log）
                self._set_status(f"'{data['word']}' added — {_reasons_text(self._reasons)}", "warn")
            else:
                self._set_status(f"'{data['word']}' added!", "ok")
            self.word_input.clear()
            self.assoc_input.clear()
            tooltip(f"'{data['word']}' added to {DECK_NAME}", period=2000)
        except Exception as e:
            self._set_status(f"Error: {e}", "warn")
        finally:
            self.add_btn.setEnabled(True)
            self.progress_bar.setVisible(False)
            self._end_batch()

    def _on_error(self, msg):
        self._stop_live()
        self._set_status(f"Error: {msg}", "warn")
        self.add_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._end_batch()


def open_dialog():
    _show_nonmodal(AddWordDialog)
