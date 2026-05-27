"""
My Word Adder — add English words to My_Daily_English with auto-fill.
Tools > Add English Word… (⌘D / Ctrl+D)  ·  Complete Missing Cards (⌘S / Ctrl+S)
"""

import os
import re
import json
import html
import subprocess
import urllib.request
import urllib.parse
import urllib.error

from aqt import mw
from aqt.qt import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QListWidget,
    QTreeWidget, QTreeWidgetItem, QWidget,
    QKeySequenceEdit, QKeySequence,
    QMessageBox, QInputDialog,
    Qt, QThread, pyqtSignal,
)
from aqt.utils import showWarning, tooltip

DECK_NAME    = "My_Daily_English"
MODEL_NAME   = "English_White_Method"
ANKI_URL     = "http://127.0.0.1:8765"
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:26b"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_KEY_PATH = os.path.expanduser("~/Workspace/Anki/.groq_key")
PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]
VENV_PYTHON     = os.path.expanduser("~/Workspace/Anki/.venv/bin/python")
GTTS_SCRIPT     = os.path.expanduser("~/Workspace/Anki/_gtts_helper.py")
IMAGE_SCRIPT    = os.path.expanduser("~/Workspace/Anki/_image_helper.py")
VALIDATE_SCRIPT = os.path.expanduser("~/Workspace/Anki/_validate_helper.py")
VOICE_WORD     = "en-US-AndrewNeural"
VOICE_SENTENCE = "en-US-AvaNeural"


def _load_groq_key():
    try:
        with open(GROQ_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get("GROQ_API_KEY", "")


def _clean_text(raw, *, lower=False):
    text = html.unescape(re.sub(r"<[^>]+>", "", raw)).replace("\xa0", " ").strip()
    return text.lower() if lower else text


# ── background worker ────────────────────────────────────────────────────────

class Worker(QThread):
    progress = pyqtSignal(str)
    step     = pyqtSignal(str, str)   # (field key, state: "ok" / "warn")
    finished = pyqtSignal(dict)
    error    = pyqtSignal(str)

    def __init__(self, word, association, media_dir):
        super().__init__()
        self.word        = word
        self.association = association
        self.media_dir   = media_dir

    def run(self):
        try:
            word = self.word
            import threading

            sentence, engine = self._llm_sentence(word)
            if not sentence:
                sentence = f"Please add an example sentence for '{word}'."
            self.step.emit("sentence", "ok" if not any(p in sentence for p in PLACEHOLDERS) else "warn")

            # Image, Translation and Audio in parallel
            image_result = [None]
            translation_result = [""]
            audio_filename = f"{word}_tts.mp3"
            front_audio_filename = f"{word}_word.mp3"

            def do_image():
                image_result[0] = self._fetch_image(word, definition=self.association, sentence=sentence)

            def do_translate():
                translation_result[0] = self._groq_translate(word, sentence)

            img_thread = threading.Thread(target=do_image)
            trans_thread = threading.Thread(target=do_translate)
            img_thread.start()
            trans_thread.start()

            audio_items = [
                {"text": word, "filepath": os.path.join(self.media_dir, front_audio_filename), "voice": VOICE_WORD},
                {"text": sentence, "filepath": os.path.join(self.media_dir, audio_filename), "voice": VOICE_SENTENCE},
            ]
            self._make_audio_batch(audio_items)
            self.step.emit("audio", "ok")

            img_thread.join()
            self.step.emit("image", "ok" if image_result[0] else "warn")
            trans_thread.join()
            self.step.emit("translation", "ok" if translation_result[0] else "warn")
            image_field = image_result[0]

            self.finished.emit({
                "word":        word,
                "association": self.association,
                "sentence":    sentence,
                "image_field": image_field,
                "translation": translation_result[0],
                "audio_filename": audio_filename,
                "front_audio_filename": front_audio_filename,
            })
        except Exception as e:
            self.error.emit(str(e))

    # ── helpers ──────────────────────────────────────────────────────────────

    def _groq_sentence(self, word):
        key = _load_groq_key()
        if not key:
            return ""
        prompt = f'Write one short, natural English example sentence using "{word}" in context. Output only the sentence, no explanation.'
        payload = json.dumps({
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 200,
        }).encode()
        try:
            req = urllib.request.Request(GROQ_API_URL, data=payload,
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bearer {key}",
                                       "User-Agent": "AnkiWordAdder/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                result = json.loads(r.read().decode())
                return result["choices"][0]["message"]["content"].strip()
        except Exception:
            return ""

    def _ollama_sentence(self, word):
        payload = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": f'Write one short, natural English example sentence using "{word}" in context. Output only the sentence, no explanation.',
            "stream": False,
        }).encode()
        try:
            req = urllib.request.Request(OLLAMA_URL, data=payload,
                              headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                result = json.loads(r.read().decode()).get("response", "").strip()
                return result if len(result) > 10 else ""
        except urllib.error.URLError:
            return ""
        except Exception:
            return ""

    def _llm_sentence(self, word):
        result = self._groq_sentence(word)
        if result and len(result) > 10:
            return result, "Groq"
        result = self._ollama_sentence(word)
        if result and len(result) > 10:
            return result, "Ollama"
        return "", "failed"

    def _groq_translate(self, word, sentence):
        """Traditional Chinese translation of word in context. Returns '' on failure."""
        key = _load_groq_key()
        if not key:
            return ""
        prompt = (f'Translate the English word "{word}" (used in: "{sentence}") into '
                  f'Traditional Chinese. Output only the Chinese translation, '
                  f'1-4 characters, no explanation.')
        payload = json.dumps({
            "model": GROQ_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "max_tokens": 20,
        }).encode()
        try:
            req = urllib.request.Request(GROQ_API_URL, data=payload,
                              headers={"Content-Type": "application/json",
                                       "Authorization": f"Bearer {key}",
                                       "User-Agent": "AnkiWordAdder/1.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
        except Exception:
            return ""

    def _fetch_image(self, word, definition="", sentence=""):
        filename = f"{word}_img_{int(__import__('time').time())}.jpg"
        filepath = os.path.join(self.media_dir, filename)
        cmd = [VENV_PYTHON, IMAGE_SCRIPT]
        if definition:
            cmd.extend(["--definition", definition])
        if sentence:
            cmd.extend(["--sentence", sentence])
        cmd.extend(["--", word, filepath])
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0:
            html = f'<img src="{filename}">'
            for line in result.stdout.splitlines():
                if line.startswith("ATTRIBUTION: "):
                    html += line[len("ATTRIBUTION: "):]
                    break
            return html
        return ""

    def _make_audio(self, text, filepath, voice=VOICE_SENTENCE):
        result = subprocess.run(
            [VENV_PYTHON, GTTS_SCRIPT, text, filepath, voice],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            raise RuntimeError(f"TTS failed: {result.stderr.strip()}")

    def _make_audio_batch(self, items):
        result = subprocess.run(
            [VENV_PYTHON, GTTS_SCRIPT, "--batch", json.dumps(items)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"TTS batch failed: {result.stderr.strip()}")


# ── dialog ───────────────────────────────────────────────────────────────────

class AddWordDialog(QDialog):
    # fields generated per add, shown as a row of boxes that flip to ✓ when done
    FIELD_BOXES = [("sentence", "Sentence"), ("image", "Image"),
                   ("audio", "Audio"), ("translation", "翻譯")]
    _BOX_STYLE = {
        "working": ("border:1.5px solid #94a3b8; border-radius:6px; padding:6px 12px; color:#64748b;", "{} …"),
        "ok":      ("border:1.5px solid #16a34a; border-radius:6px; padding:6px 12px; color:#16a34a; font-weight:600;", "✓ {}"),
        "warn":    ("border:1.5px solid #ea580c; border-radius:6px; padding:6px 12px; color:#ea580c; font-weight:600;", "⚠ {}"),
    }
    _STATUS_STYLE = {
        "info": "font-size:13px; color:#64748b;",
        "ok":   "font-size:18px; color:#16a34a; font-weight:700; padding:6px;",
        "warn": "font-size:14px; color:#ea580c; font-weight:600;",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add English Word")
        self.setMinimumWidth(440)
        self._worker = None
        self._setup_ui()

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
        boxes_row = QHBoxLayout()
        for key, label in self.FIELD_BOXES:
            box = QLabel(label)
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.setVisible(False)
            self._boxes[key] = box
            boxes_row.addWidget(box)
        root.addLayout(boxes_row)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
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

        self.word_input.returnPressed.connect(self._on_add)

    def _set_box(self, key, state):
        box = self._boxes.get(key)
        if not box:
            return
        style, fmt = self._BOX_STYLE[state]
        box.setStyleSheet(style)
        box.setText(fmt.format(dict(self.FIELD_BOXES)[key]))

    def _start_boxes(self):
        for key in self._boxes:
            self._boxes[key].setVisible(True)
            self._set_box(key, "working")

    def _set_status(self, text, kind="info"):
        self.status.setStyleSheet(self._STATUS_STYLE[kind])
        self.status.setText(text)

    def _check_word_api(self, word):
        url = "https://api.dictionaryapi.dev/api/v2/entries/en/" + urllib.parse.quote(word)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read().decode())
                return isinstance(data, list) and len(data) > 0
        except Exception:
            return None  # network error — skip validation

    def _validate_word_ui(self, word):
        """Returns corrected word, or None if user cancelled."""
        found = self._check_word_api(word)
        if found is True:
            return word
        if found is None:
            return word  # API unavailable, proceed anyway

        # Not found — get spell suggestions via venv subprocess
        suggestions = []
        try:
            result = subprocess.run(
                [VENV_PYTHON, VALIDATE_SCRIPT, "word", word],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout.strip())
                suggestions = data.get("suggestions", [])
        except Exception:
            pass

        if suggestions:
            items = suggestions + [f"Keep '{word}' as-is"]
            choice, ok = QInputDialog.getItem(
                self, "Word Not Found",
                f"'{word}' was not found in the dictionary.\nDid you mean:",
                items, 0, False,
            )
            if not ok:
                return None
            return word if choice == f"Keep '{word}' as-is" else choice
        else:
            reply = QMessageBox.question(
                self, "Word Not Found",
                f"'{word}' was not found in the dictionary.\nContinue anyway?",
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

        self._set_status("Checking word…")
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
               for nid in mw.col.find_notes(f'deck:"{DECK_NAME}"')):
            self._set_status(f"⚠ '{word}' already exists in the deck.", "warn")
            return

        self.add_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._set_status(f"生成中：{word}")
        self._start_boxes()

        self._worker = Worker(word, assoc, mw.col.media.dir())
        self._worker.step.connect(self._set_box)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_finished(self, data):
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

            deck_id = mw.col.decks.id(DECK_NAME)
            mw.col.add_note(note, deck_id)
            mw.col.save()
            mw.reset()

            self._set_status(f"✓ '{data['word']}' added!", "ok")
            self.word_input.clear()
            self.assoc_input.clear()
            tooltip(f"'{data['word']}' added to {DECK_NAME}", period=2000)
        except Exception as e:
            self._set_status(f"Error: {e}", "warn")
        finally:
            self.add_btn.setEnabled(True)
            self.progress_bar.setVisible(False)

    def _on_error(self, msg):
        self._set_status(f"Error: {msg}", "warn")
        self.add_btn.setEnabled(True)
        self.progress_bar.setVisible(False)


# ── backfill worker ───────────────────────────────────────────────────────────

MAX_BACKFILL_WORKERS = 3


class BackfillWorker(QThread):
    progress = pyqtSignal(str)
    finished = pyqtSignal(list)
    error    = pyqtSignal(str)

    def __init__(self, notes, media_dir):
        super().__init__()
        self.notes     = notes
        self.media_dir = media_dir
        self._w = Worker.__new__(Worker)
        self._w.media_dir = media_dir
        self._card_status = {}
        self._lock = __import__('threading').Lock()

    def _update_status(self, word, step):
        with self._lock:
            self._card_status[word] = step
            lines = []
            for w, s in self._card_status.items():
                lines.append(f"{w}:  {s}")
            self.progress.emit("\n".join(lines))

    def _process_one(self, note):
        raw  = note["fields"]["Front"]["value"]
        word = _clean_text(raw, lower=True)
        note_id = note["noteId"]

        self._update_status(word, "Sentence…")
        fields = {}
        engine = ""

        current = note["fields"]["Sentence"]["value"]
        if not current or any(p in current for p in PLACEHOLDERS):
            sentence, engine = self._w._llm_sentence(word)
            if not sentence:
                sentence = f"Please add an example sentence for '{word}'."
            fields["Sentence"] = sentence
        else:
            sentence = _clean_text(current)
            engine = "kept"

        s_icon = "✅" if not any(p in sentence for p in PLACEHOLDERS) else "⚠️"
        self._update_status(word, f"{s_icon} Sentence ({engine}) → Image + Audio + 翻譯…")

        import threading
        need_image = "<img" not in note["fields"]["Image_Prompt"]["value"]
        need_audio = not note["fields"]["Audio"]["value"]
        front_audio_val = note["fields"].get("Front_Audio", {}).get("value", "")
        need_front = not front_audio_val
        need_translation = not note["fields"].get("Translation", {}).get("value", "")

        image_result = [None]
        translation_result = [""]
        img_thread = None
        trans_thread = None

        if need_translation:
            def do_translate(w=word, s=sentence):
                translation_result[0] = self._w._groq_translate(w, s)
            trans_thread = threading.Thread(target=do_translate)
            trans_thread.start()

        if need_image:
            association = _clean_text(note["fields"].get("Association", {}).get("value", ""))
            def do_image(w=word, a=association, s=sentence):
                image_result[0] = self._w._fetch_image(w, definition=a, sentence=s)
            img_thread = threading.Thread(target=do_image)
            img_thread.start()

        audio_batch = []
        if need_audio:
            audio_filename = f"{word}_tts.mp3"
            audio_batch.append({"text": sentence, "filepath": os.path.join(self.media_dir, audio_filename), "voice": VOICE_SENTENCE})
            fields["Audio"] = f"[sound:{audio_filename}]"
        if need_front:
            front_filename = f"{word}_word.mp3"
            audio_batch.append({"text": word, "filepath": os.path.join(self.media_dir, front_filename), "voice": VOICE_WORD})
            fields["Front_Audio"] = f"[sound:{front_filename}]"
        if audio_batch:
            self._w._make_audio_batch(audio_batch)

        if img_thread:
            img_thread.join()
            fields["Image_Prompt"] = image_result[0] or ""
            i_icon = "✅" if image_result[0] else "⚠️"
        else:
            i_icon = "✅"

        if trans_thread:
            trans_thread.join()
            if translation_result[0]:
                fields["Translation"] = translation_result[0]

        if fields:
            payload = json.dumps({
                "action": "updateNoteFields", "version": 6,
                "params": {"note": {"id": note_id, "fields": fields}}
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(ANKI_URL, data=payload,
                            headers={"Content-Type": "application/json"}),
                timeout=5,
            )

        t_icon = "✅" if translation_result[0] else "⚠️"
        self._update_status(word, f"{s_icon} Sentence  {i_icon} Image  ✅ Audio  {t_icon} 翻譯")
        return f"✓ {word}" if fields else f"— {word}"

    def run(self):
        from concurrent.futures import ThreadPoolExecutor, as_completed
        results = []
        with ThreadPoolExecutor(max_workers=MAX_BACKFILL_WORKERS) as pool:
            futures = {pool.submit(self._process_one, note): note for note in self.notes}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    note = futures[future]
                    word = _clean_text(note["fields"]["Front"]["value"], lower=True)
                    self._update_status(word, f"✗ {e}")
                    results.append(f"✗ {word}: {e}")
        self.finished.emit(results)


# ── backfill dialog ───────────────────────────────────────────────────────────

class BackfillDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Complete Missing Cards")
        self.setMinimumWidth(460)
        self.setMinimumHeight(320)
        self._worker = None
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)

        root.addWidget(QLabel("Cards missing Sentence / Image / Audio / Front Audio / Translation:"))

        self.list_widget = QListWidget()
        root.addWidget(self.list_widget)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        btns = QHBoxLayout()
        self.run_btn = QPushButton("Complete All")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._on_run)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(self.run_btn)
        btns.addWidget(close_btn)
        root.addLayout(btns)

    def _scan(self):
        ids   = mw.col.find_notes(f'deck:"{DECK_NAME}"')
        notes = []
        for nid in ids:
            note = mw.col.get_note(nid)
            bad_sentence = any(p in note["Sentence"] for p in PLACEHOLDERS)
            front_audio = note["Front_Audio"] if "Front_Audio" in note else ""
            translation = note["Translation"] if "Translation" in note else ""
            has_img = "<img" in note["Image_Prompt"]
            if not note["Sentence"] or bad_sentence or not note["Audio"] or not has_img or not front_audio or not translation:
                raw  = note["Front"]
                word = _clean_text(raw)
                self.list_widget.addItem(word)
                notes.append({
                    "noteId": nid,
                    "fields": {
                        "Front":        {"value": note["Front"]},
                        "Association":  {"value": note["Association"]},
                        "Sentence":     {"value": note["Sentence"]},
                        "Image_Prompt": {"value": note["Image_Prompt"]},
                        "Audio":        {"value": note["Audio"]},
                        "Front_Audio":  {"value": front_audio},
                        "Translation":  {"value": translation},
                    }
                })
        self._pending_notes = notes
        if notes:
            self.status.setText(f"{len(notes)} card(s) need filling.")
            self.run_btn.setEnabled(True)
        else:
            self.status.setText("All cards are complete!")

    def _on_run(self):
        self.run_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._worker = BackfillWorker(self._pending_notes, mw.col.media.dir())
        self._worker.progress.connect(self.status.setText)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(lambda e: self.status.setText(f"Error: {e}"))
        self._worker.start()

    def _on_finished(self, results):
        self.progress_bar.setVisible(False)
        self.list_widget.clear()
        for r in results:
            self.list_widget.addItem(r)
        mw.col.save()
        mw.reset()
        ok = sum(1 for r in results if r.startswith("✓"))
        self.status.setText(f"Done — {ok} card(s) updated. Remember to sync Anki!")
        self.run_btn.setEnabled(False)


# ── find duplicates dialog ─────────────────────────────────────────────────────

class FindDuplicatesDialog(QDialog):
    """Find cards whose Front is the same after normalization (HTML/case-insensitive),
    and let the user pick which to delete. Catches dupes that slipped in via mobile."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find Duplicate Words")
        self.setMinimumWidth(540)
        self.setMinimumHeight(420)
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("正規化後 Front 相同的重複卡片。勾選要刪除的（每組至少保留一張）："))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["單字 / 卡片", "例句"])
        self.tree.setColumnWidth(0, 220)
        root.addWidget(self.tree)

        self.status = QLabel("")
        root.addWidget(self.status)

        btns = QHBoxLayout()
        self.del_btn = QPushButton("刪除勾選的卡片")
        self.del_btn.setEnabled(False)
        self.del_btn.clicked.connect(self._on_delete)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(self.del_btn)
        btns.addWidget(close_btn)
        root.addLayout(btns)

    def _scan(self):
        from collections import defaultdict
        self.tree.clear()

        groups = defaultdict(list)
        for nid in mw.col.find_notes(f'deck:"{DECK_NAME}"'):
            note = mw.col.get_note(nid)
            key = _clean_text(note["Front"], lower=True)
            if key:
                groups[key].append((nid, note))
        dup_groups = {k: v for k, v in groups.items() if len(v) > 1}

        total = 0
        for key, items in sorted(dup_groups.items()):
            parent = QTreeWidgetItem(self.tree, [f"{key}  ({len(items)} 張)", ""])
            parent.setFlags(parent.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            parent.setExpanded(True)
            for nid, note in items:
                sentence = _clean_text(note["Sentence"]) if "Sentence" in note else ""
                child = QTreeWidgetItem(parent, [_clean_text(note["Front"]) or key, sentence[:70]])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Unchecked)
                child.setData(0, Qt.ItemDataRole.UserRole, nid)
                total += 1

        if dup_groups:
            self.status.setText(f"找到 {len(dup_groups)} 組重複，共 {total} 張卡片。")
            self.del_btn.setEnabled(True)
        else:
            self.status.setText("沒有重複卡片 ✓")
            self.del_btn.setEnabled(False)

    def _on_delete(self):
        to_delete = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            checked = [parent.child(j).data(0, Qt.ItemDataRole.UserRole)
                       for j in range(parent.childCount())
                       if parent.child(j).checkState(0) == Qt.CheckState.Checked]
            if checked and len(checked) == parent.childCount():
                self.status.setText(f"⚠ 「{parent.text(0)}」整組都勾選了，每組至少要留一張。")
                return
            to_delete.extend(checked)

        if not to_delete:
            self.status.setText("沒有勾選任何卡片。")
            return

        reply = QMessageBox.question(
            self, "確認刪除",
            f"確定刪除勾選的 {len(to_delete)} 張卡片？此動作無法復原。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        mw.col.remove_notes(to_delete)
        mw.col.save()
        mw.reset()
        self._scan()
        self.status.setText(f"✓ 已刪除 {len(to_delete)} 張。記得同步 Anki！")


# ── menu entries ──────────────────────────────────────────────────────────────

def open_dialog():
    AddWordDialog(mw).exec()

def open_backfill_dialog():
    BackfillDialog(mw).exec()

def open_duplicates_dialog():
    FindDuplicatesDialog(mw).exec()

DEFAULT_SHORTCUTS = {"add": "Ctrl+D", "complete": "Ctrl+S", "find_duplicates": "Ctrl+F"}
ACTIONS = {}  # key -> QAction, so the settings dialog can re-bind shortcuts live


def _shortcut(key):
    """Current shortcut from addon config; empty string = no shortcut (menu only)."""
    cfg = mw.addonManager.getConfig(__name__) or {}
    return cfg.get("shortcuts", {}).get(key, DEFAULT_SHORTCUTS[key])


def _add_menu_action(title, key, handler):
    act = QAction(title, mw)
    sc = _shortcut(key)
    if sc:
        act.setShortcut(sc)
    act.triggered.connect(handler)
    mw.form.menuTools.addAction(act)
    ACTIONS[key] = act


class SettingsDialog(QDialog):
    """Friendly shortcut editor — press a key combo per action, no JSON, applies live."""

    LABELS = [
        ("add", "Add English Word"),
        ("complete", "Complete Missing Cards"),
        ("find_duplicates", "Find Duplicate Words"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("My Word Adder — 快捷鍵設定")
        self.setMinimumWidth(440)
        self._edits = {}
        self._setup_ui()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("點欄位後直接按你要的組合鍵；按「清除」＝不綁，只走選單。"))

        form = QFormLayout()
        for key, title in self.LABELS:
            edit = QKeySequenceEdit(QKeySequence(_shortcut(key)))
            edit.setMaximumSequenceLength(1)
            edit.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # only arm when clicked, not on open
            self._edits[key] = edit

            clear = QPushButton("清除")
            clear.clicked.connect(lambda _, e=edit: e.clear())
            row = QHBoxLayout()
            row.addWidget(edit)
            row.addWidget(clear)
            wrap = QWidget()
            wrap.setLayout(row)
            form.addRow(f"{title}：", wrap)
        root.addLayout(form)

        btns = QHBoxLayout()
        save = QPushButton("儲存")
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        btns.addWidget(save)
        btns.addWidget(cancel)
        root.addLayout(btns)

        cancel.setFocus()  # start with focus off the key fields — nothing armed

    def _on_save(self):
        cfg = mw.addonManager.getConfig(__name__) or {}
        sc = cfg.setdefault("shortcuts", {})
        for key, edit in self._edits.items():
            sc[key] = edit.keySequence().toString()
        mw.addonManager.writeConfig(__name__, cfg)
        for key, act in ACTIONS.items():          # apply live — no restart needed
            act.setShortcut(QKeySequence(sc.get(key, DEFAULT_SHORTCUTS[key])))
        tooltip("快捷鍵已更新", period=2000)
        self.accept()


def open_settings_dialog():
    SettingsDialog(mw).exec()


_add_menu_action("Add English Word…", "add", open_dialog)
_add_menu_action("Complete Missing Cards…", "complete", open_backfill_dialog)
_add_menu_action("Find Duplicate Words…", "find_duplicates", open_duplicates_dialog)

_settings_action = QAction("My Word Adder Settings…", mw)
_settings_action.triggered.connect(open_settings_dialog)
mw.form.menuTools.addAction(_settings_action)
