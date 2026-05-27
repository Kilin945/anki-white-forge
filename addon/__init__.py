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
import urllib.error

from aqt import mw
from aqt.qt import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QScrollArea,
    QTreeWidget, QTreeWidgetItem, QWidget,
    QKeySequenceEdit, QKeySequence,
    QMessageBox,
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

# field progress boxes — shared by Add (single row) and Complete (one row per card)
FIELD_BOXES = [("sentence", "Sentence"), ("image", "Image"),
               ("audio", "Audio"), ("translation", "翻譯")]
BOX_STYLE = {  # text is just the field label; state shown by colour only (no ✓ / ⚠)
    "working": ("border:1.5px solid #94a3b8; border-radius:6px; padding:6px 12px; color:#64748b;", "{}"),
    "ok":      ("border:1.5px solid #16a34a; border-radius:6px; padding:6px 12px; color:#16a34a; font-weight:600;", "{}"),
    "warn":    ("border:1.5px solid #ea580c; border-radius:6px; padding:6px 12px; color:#ea580c; font-weight:600;", "{}"),
}
_FIELD_LABEL = dict(FIELD_BOXES)


def _load_groq_key():
    try:
        with open(GROQ_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get("GROQ_API_KEY", "")


def _clean_text(raw, *, lower=False):
    # strip only real HTML tags (`<tag ...>` / `</tag>`); leave literal `<`…`>` in content
    text = html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", raw)).replace("\xa0", " ").strip()
    return text.lower() if lower else text


ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\- ]*")


def _looks_english(word):
    """True if plausibly English (letters + space / - / '); rejects CJK, digits, symbols.
    Shared charset gate for both ⌘D (add) and ⌘S (complete)."""
    return bool(ENGLISH_WORD_RE.fullmatch((word or "").strip()))


def _deck_note_ids():
    """Note ids in the deck restricted to our note type, so deck scans never touch a
    stray note type (e.g. a Cloze card) that lacks our fields and would KeyError."""
    return mw.col.find_notes(f'deck:"{DECK_NAME}" note:"{MODEL_NAME}"')


def _groq_chat(prompt, *, temperature, max_tokens, timeout):
    """POST one user prompt to Groq; return the stripped reply, or '' on no key / any failure."""
    key = _load_groq_key()
    if not key:
        return ""
    payload = json.dumps({
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    try:
        req = urllib.request.Request(GROQ_API_URL, data=payload,
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {key}",
                           "User-Agent": "AnkiWordAdder/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


def _groq_spellcheck(word):
    """Spell-check a word/phrase via Groq. Returns:
      ("ok", None)          correctly spelled English word/phrase
      ("typo", suggestion)  misspelled — with the single best correction
      ("nonword", None)     gibberish / not an English word at all
      ("unknown", None)     Groq unavailable / couldn't decide
    """
    prompt = (
        f'You are an English spell checker. The user typed: "{word}".\n'
        f'- If it is a correctly spelled English word or common phrase, reply exactly: OK\n'
        f'- If it is a misspelling of a real English word, reply only the single correct spelling.\n'
        f'- If it is not an English word at all (random letters / gibberish), reply exactly: NONWORD\n'
        f'Reply with only OK, NONWORD, or the corrected word — no other text.'
    )
    reply = _groq_chat(prompt, temperature=0, max_tokens=12, timeout=8)
    if not reply:
        return ("unknown", None)
    cleaned = reply.strip().strip('".').strip().lower()
    if cleaned in ("ok", word.lower()):
        return ("ok", None)
    if cleaned == "nonword":
        return ("nonword", None)
    if cleaned and re.fullmatch(r"[a-z][a-z'\- ]*", cleaned):
        return ("typo", cleaned)
    return ("unknown", None)


# ── background worker ────────────────────────────────────────────────────────

class Worker(QThread):
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
            try:
                self._make_audio_batch(audio_items)
                self.step.emit("audio", "ok")
            finally:
                img_thread.join()          # always join so threads don't leak on audio failure
                trans_thread.join()

            self.step.emit("image", "ok" if image_result[0] else "warn")
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
        prompt = f'Write one short, natural English example sentence using "{word}" in context. Output only the sentence, no explanation.'
        return _groq_chat(prompt, temperature=0.7, max_tokens=200, timeout=15)

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
        prompt = (f'Translate the English word "{word}" (used in: "{sentence}") into '
                  f'Traditional Chinese. Output only the Chinese translation, '
                  f'1-4 characters, no explanation.')
        reply = _groq_chat(prompt, temperature=0.3, max_tokens=20, timeout=10)
        # reject implausible output → leave empty so ⌘S re-generates it later
        if re.search(r"[A-Za-z]", reply):                    # English preamble / refusal / paren
            return ""
        if len(re.findall(r"[一-鿿]", reply)) > 6:   # >6 漢字 = a sentence, not a word
            return ""
        return reply

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

    def _make_audio_batch(self, items):
        result = subprocess.run(
            [VENV_PYTHON, GTTS_SCRIPT, "--batch", json.dumps(items)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"TTS batch failed: {result.stderr.strip()}")


# ── dialog ───────────────────────────────────────────────────────────────────

class AddWordDialog(QDialog):
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
        for key, label in FIELD_BOXES:
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
        style, fmt = BOX_STYLE[state]
        box.setStyleSheet(style)
        box.setText(fmt.format(_FIELD_LABEL[key]))

    def _start_boxes(self):
        for key in self._boxes:
            self._boxes[key].setVisible(True)
            self._set_box(key, "working")

    def _set_status(self, text, kind="info"):
        self.status.setStyleSheet(self._STATUS_STYLE[kind])
        self.status.setText(text)

    def _spellcheck(self, word):
        """(status, suggestion) — Groq primary, offline pyspellchecker fallback."""
        status, suggestion = _groq_spellcheck(word)
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
            showWarning(f"'{word}' 包含非英文字元，無法建立。")
            return None

        # Layer 2 — spelling (Groq, offline fallback)
        status, suggestion = self._spellcheck(word)
        if status == "ok":
            return word

        if status == "typo" and suggestion and suggestion != word:
            box = QMessageBox(self)
            box.setWindowTitle("拼字檢查")
            box.setText(f"'{word}' 可能拼錯了，您是不是想用 '{suggestion}'？")
            use_btn  = box.addButton(f"改用 '{suggestion}'", QMessageBox.ButtonRole.AcceptRole)
            keep_btn = box.addButton(f"仍用 '{word}'", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
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
            self, "查不到這個字",
            f"'{word}' 查不到、可能拼錯，確定要建立嗎？",
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

        self._set_status("檢查拼字中…")
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

            self._set_status(f"'{data['word']}' added!", "ok")
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


class FieldRow(QWidget):
    """One card's progress: word + Sentence/Image/Audio/翻譯 boxes + 'added!' badge.
    Fields already present start green; missing ones start grey and flip on completion."""

    def __init__(self, word, present, parent=None):
        super().__init__(parent)
        self.word = word
        self._boxes = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        wl = QLabel(word)
        wl.setMinimumWidth(120)
        wl.setStyleSheet("font-weight:600; color:#1E293B;")
        lay.addWidget(wl)
        for key, _label in FIELD_BOXES:
            box = QLabel()
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._boxes[key] = box
            lay.addWidget(box)
            self.set_box(key, "ok" if present.get(key) else "working")
        self.badge = QLabel("")
        self.badge.setStyleSheet("color:#16a34a; font-weight:700; padding-left:8px;")
        lay.addWidget(self.badge)
        lay.addStretch()

    def set_box(self, key, state):
        box = self._boxes.get(key)
        if not box:
            return
        style, fmt = BOX_STYLE[state]
        box.setStyleSheet(style)
        box.setText(fmt.format(_FIELD_LABEL[key]))

    def set_done(self):
        self.badge.setText(f"'{self.word}' added!")


# ── backfill worker ───────────────────────────────────────────────────────────

MAX_BACKFILL_WORKERS = 3


class BackfillWorker(QThread):
    step      = pyqtSignal(int, str, str)   # (note_id, field, "ok"/"warn")
    card_done = pyqtSignal(int)             # (note_id) finished successfully
    finished  = pyqtSignal(list)
    error     = pyqtSignal(str)

    def __init__(self, notes, media_dir):
        super().__init__()
        self.notes     = notes
        self.media_dir = media_dir
        self._w = Worker.__new__(Worker)
        self._w.media_dir = media_dir

    def _process_one(self, note):
        note_id = note["noteId"]
        word = _clean_text(note["fields"]["Front"]["value"], lower=True)
        fields = {}

        current = note["fields"]["Sentence"]["value"]
        if not current or any(p in current for p in PLACEHOLDERS):
            sentence, _ = self._w._llm_sentence(word)
            if not sentence:
                sentence = f"Please add an example sentence for '{word}'."
            fields["Sentence"] = sentence
            self.step.emit(note_id, "sentence",
                           "ok" if not any(p in sentence for p in PLACEHOLDERS) else "warn")
        else:
            sentence = _clean_text(current)

        import threading
        need_image = "<img" not in note["fields"]["Image_Prompt"]["value"]
        need_audio = not note["fields"]["Audio"]["value"]
        need_front = not note["fields"].get("Front_Audio", {}).get("value", "")
        need_translation = not note["fields"].get("Translation", {}).get("value", "")

        image_result = [None]
        translation_result = [""]
        img_thread = trans_thread = None

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
        try:
            if audio_batch:
                self._w._make_audio_batch(audio_batch)
                self.step.emit(note_id, "audio", "ok")
        finally:
            if img_thread:
                img_thread.join()
            if trans_thread:
                trans_thread.join()

        if need_image:
            fields["Image_Prompt"] = image_result[0] or ""
            self.step.emit(note_id, "image", "ok" if image_result[0] else "warn")
        if need_translation:
            if translation_result[0]:
                fields["Translation"] = translation_result[0]
            self.step.emit(note_id, "translation", "ok" if translation_result[0] else "warn")

        if fields:
            payload = json.dumps({
                "action": "updateNoteFields", "version": 6,
                "params": {"note": {"id": note_id, "fields": fields}}
            }).encode()
            with urllib.request.urlopen(
                urllib.request.Request(ANKI_URL, data=payload,
                            headers={"Content-Type": "application/json"}),
                timeout=15,
            ) as resp:
                err = json.loads(resp.read().decode()).get("error")
            if err:
                raise RuntimeError(f"AnkiConnect: {err}")

        self.card_done.emit(note_id)
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
                    results.append(f"✗ {word}: {e}")
        self.finished.emit(results)


# ── backfill dialog ───────────────────────────────────────────────────────────

class BackfillDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Complete Missing Cards")
        self.setMinimumWidth(720)
        self.setMinimumHeight(380)
        self._worker = None
        self._rows = {}
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Cards missing Sentence / Image / Audio / Translation:"))

        self._rows_host = QWidget()
        self._rows_box = QVBoxLayout(self._rows_host)
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_host)
        root.addWidget(scroll)

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
        notes = []
        invalid = 0
        for nid in _deck_note_ids():
            note = mw.col.get_note(nid)
            bad_sentence = any(p in note["Sentence"] for p in PLACEHOLDERS)
            front_audio = note["Front_Audio"] if "Front_Audio" in note else ""
            translation = note["Translation"] if "Translation" in note else ""
            has_img = "<img" in note["Image_Prompt"]
            audio_ok = bool(note["Audio"]) and bool(front_audio)
            incomplete = (not note["Sentence"] or bad_sentence or not note["Audio"]
                          or not has_img or not front_audio or not translation)
            if not incomplete:
                continue

            word = _clean_text(note["Front"])
            if not _looks_english(word):       # not English → don't fill, just flag it
                invalid += 1
                lbl = QLabel(f"{word or note['Front']}（包含非英文字元，無法建立）")
                lbl.setStyleSheet("color:#ea580c; padding:4px;")
                self._rows_box.addWidget(lbl)
                continue

            present = {
                "sentence": bool(note["Sentence"]) and not bad_sentence,
                "image": has_img,
                "audio": audio_ok,
                "translation": bool(translation),
            }
            row = FieldRow(word, present)
            self._rows_box.addWidget(row)
            self._rows[nid] = row
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
        self._rows_box.addStretch()
        self._pending_notes = notes
        parts = []
        if notes:
            parts.append(f"{len(notes)} card(s) need filling.")
        if invalid:
            parts.append(f"{invalid} 張包含非英文字元、無法建立（請修正或刪除）。")
        self.status.setText(" ".join(parts) if parts else "All cards are complete!")
        self.run_btn.setEnabled(bool(notes))

    def _on_run(self):
        self.run_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._worker = BackfillWorker(self._pending_notes, mw.col.media.dir())
        self._worker.step.connect(self._on_step)
        self._worker.card_done.connect(self._on_card_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(lambda e: self.status.setText(f"Error: {e}"))
        self._worker.start()

    def _on_step(self, note_id, field, state):
        row = self._rows.get(note_id)
        if row:
            row.set_box(field, state)

    def _on_card_done(self, note_id):
        row = self._rows.get(note_id)
        if row:
            row.set_done()

    def _on_finished(self, results):
        self.progress_bar.setVisible(False)
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
        for nid in _deck_note_ids():
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
        new = {key: edit.keySequence().toString() for key, edit in self._edits.items()}
        used = [s for s in new.values() if s]
        if len(used) != len(set(used)):       # same combo on two actions = ambiguous, neither fires
            showWarning("兩個功能設了相同的快捷鍵，請改成不同的。")
            return
        cfg = mw.addonManager.getConfig(__name__) or {}
        sc = cfg.setdefault("shortcuts", {})
        sc.update(new)
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
