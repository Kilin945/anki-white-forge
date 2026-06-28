"""
My Word Adder — add English words to My_Daily_English with auto-fill.
Tools > Add English Word… (⌘A / Ctrl+A)  ·  Complete Missing Cards (⌘S / Ctrl+S)
"""

import os
import re
import json
import html
import time
import threading
import subprocess
import urllib.request
import urllib.error

from aqt import mw
from aqt.qt import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QPushButton, QProgressBar, QScrollArea,
    QTreeWidget, QTreeWidgetItem, QWidget, QFrame, QCheckBox,
    QKeySequenceEdit, QKeySequence,
    QMessageBox,
    Qt, QThread, pyqtSignal,
)
from aqt.utils import showWarning, tooltip

DECK_NAME    = "My_Daily_English"
MODEL_NAME   = "English_White_Method"
ANKI_URL     = "http://127.0.0.1:8765"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL   = "llama-3.3-70b-versatile"
GROQ_KEY_PATH = os.path.expanduser("~/Workspace/anki/.groq_key")
PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]
VENV_PYTHON     = os.path.expanduser("~/Workspace/anki/.venv/bin/python")
GTTS_SCRIPT     = os.path.expanduser("~/Workspace/anki/_gtts_helper.py")
IMAGE_SCRIPT    = os.path.expanduser("~/Workspace/anki/_image_helper.py")
VALIDATE_SCRIPT = os.path.expanduser("~/Workspace/anki/_validate_helper.py")
VOICE_WORD     = "en-US-AndrewNeural"
VOICE_SENTENCE = "en-US-AvaNeural"

# field progress boxes. Both ⌘D Add and ⌘S Complete show all five — ⌘S now fills
# Sentence_CN too (the everyday small case: cards added on mobile / via Anki's built-in
# Add bypass ⌘D, so ⌘S is where they get completed). Large bulk fills still go through
# the dedicated 批次回填 menu, which is paced against the rate limit.
# Order matches the processing/completion order: Sentence is generated first (everything
# else depends on it), Audio second (TTS needs the finished sentence), then Image / Meaning /
# Translation run in parallel and relay in as they finish. Two Chinese fields are
# distinguished by word-vs-sentence, not by a "CN" tag: Meaning = the word's meaning
# (Translation field), Translation = the sentence's translation (Sentence_CN field).
FIELD_BOXES = [("sentence", "Sentence"), ("audio", "Audio"), ("image", "Image"),
               ("translation", "Meaning"), ("sentence_cn", "Translation")]
BACKFILL_BOXES = FIELD_BOXES
BOX_STYLE = {  # text is just the field label; state shown by colour only (no ✓ / ⚠)
    "working": ("border:1.5px solid #94a3b8; border-radius:6px; padding:6px 8px; color:#64748b;", "{}"),
    "ok":      ("border:1.5px solid #16a34a; border-radius:6px; padding:6px 8px; color:#16a34a; font-weight:600;", "{}"),
    "warn":    ("border:1.5px solid #ea580c; border-radius:6px; padding:6px 8px; color:#ea580c; font-weight:600;", "{}"),
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


def _accept_word_translation(word, reply):
    """Validate a word-translation reply. Accept: a Chinese gloss (<=8 漢字, not a sentence,
    not buried in English preamble), OR a short English proper-noun NAME that echoes the
    input word (e.g. word 'spring' -> 'Spring Boot', 'kafka' -> 'Apache Kafka'). Reject
    refusals / preambles / junk that do not echo the word (e.g. 'None', 'I cannot translate').
    Returns the accepted reply, or '' to reject.
    KEEP IN SYNC with core/llm.py::_accept_word_translation (addon cannot import core)."""
    reply = (reply or "").strip()
    if not reply:
        return ""
    if re.search(r"[一-鿿]", reply):                       # Chinese gloss
        if len(re.findall(r"[一-鿿]", reply)) > 8:          # too long -> a sentence, not a term
            return ""
        if len(re.findall(r"[A-Za-z]{2,}", reply)) >= 3:    # Chinese + lots of English -> preamble
            return ""
        return reply
    # no Chinese -> only valid as a short proper-noun name that echoes the word
    if len(re.findall(r"[A-Za-z]+", reply)) <= 3 and word.lower() in reply.lower():
        return reply
    return ""


def _sentence_prompt(word, association=""):
    """Example-sentence prompt: pick sense (hint > SWE > everyday), short & clear, no
    definition/circular sentence.
    KEEP IN SYNC with core/llm._sentence_instructions — addon cannot import core, so this
    is a deliberate duplicate. Change one → change both."""
    hint = f'1. If a hint is given, use the sense the hint points to. Hint: "{association}"\n' if association else ""
    swe_n = "2." if association else "1."
    common_n = "3." if association else "2."
    return (
        f'You are helping a software engineer learn the English word "{word}".\n\n'
        f'Pick the meaning to teach, in this priority:\n'
        f'{hint}'
        f'{swe_n} If "{word}" has a common usage in software engineering / programming / tech, use that sense.\n'
        f'{common_n} Otherwise use its most common everyday meaning.\n\n'
        f'Then write ONE example sentence that uses "{word}" naturally and makes its meaning '
        f'obvious — someone who does not know the word should be able to guess it from the '
        f'sentence alone. Keep it SHORT: aim for about 6-12 words, ONE simple clause. Cut every '
        f'word that does not help show the meaning — no scene-setting, no subordinate '
        f'"while / which / to avoid / during ..." clauses. Only go longer if the word genuinely '
        f'cannot be shown clearly in that space. Use plain, everyday language; avoid '
        f'business/corporate phrasing. If you chose the software-engineering sense, a code/tech '
        f'situation is natural; if you chose an everyday or hint-driven sense, write a normal '
        f'everyday sentence and do NOT force in software, teams, or tech. '
        f'Do NOT write a definition or a circular sentence (no "X means ...", "X is when ...", '
        f'"{word} is a kind of ..."); show the meaning through a real, concrete situation.\n\n'
        f'Output only the sentence. No explanation, no quotes.'
    )


def _deck_note_ids():
    """Note ids in the deck restricted to our note type, so deck scans never touch a
    stray note type (e.g. a Cloze card) that lacks our fields and would KeyError."""
    return mw.col.find_notes(f'deck:"{DECK_NAME}" note:"{MODEL_NAME}"')


def _parse_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_reset_secs(s):
    """Groq reset header → seconds. Handles '1m26.4s' / '185ms' / '2.5s' / '1h2m'."""
    if not s:
        return 0.0
    return sum(float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
               for num, unit in re.findall(r"([\d.]+)(ms|s|m|h)", s))


class _GroqLimiter:
    """Adaptive rate-limit guard. Every Groq response carries the live limit + remaining
    quota (x-ratelimit-* headers); we read them and, when the per-minute token budget runs
    low, briefly wait for the bucket to refill — so bursts (⌘S) stay under the limit instead
    of crashing into 429. Self-tuning: if Groq changes the limit (or we switch providers),
    the headers reflect it, no hard-coded number. Thread-safe (⌘S calls Groq concurrently)."""

    _TOKEN_FLOOR = 1500      # stop one call short of empty → never actually 429, no waiting

    def __init__(self):
        self._lock = threading.Lock()
        self._remaining_tokens = None
        self._reset_at = 0.0

    def wall_secs(self):
        """How long until there's quota again (secs). 0 = clear to call now. The caller
        stops immediately when this is > 0 — we never silently wait."""
        with self._lock:
            if self._remaining_tokens is None or self._remaining_tokens >= self._TOKEN_FLOOR:
                return 0.0
            return max(0.0, self._reset_at - time.monotonic())

    def update(self, headers):
        """Record live remaining tokens + reset time from a response's headers."""
        if not headers:
            return
        rt = _parse_int(headers.get("x-ratelimit-remaining-tokens"))
        if rt is None:
            return
        with self._lock:
            self._remaining_tokens = rt
            self._reset_at = time.monotonic() + _parse_reset_secs(headers.get("x-ratelimit-reset-tokens"))


_groq_limiter = _GroqLimiter()


def _groq_chat(prompt, *, temperature, max_tokens, timeout, strict=False):
    """POST one user prompt to Groq; return the stripped reply, or '' on no key / any
    failure. strict=True re-raises HTTP 429 as _AddonRateLimited (so the burst engine can
    pace/stop) instead of swallowing it as ''."""
    key = _load_groq_key()
    if not key:
        return ""
    payload = json.dumps({
        "model": GROQ_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }).encode()
    req = urllib.request.Request(GROQ_API_URL, data=payload,
              headers={"Content-Type": "application/json",
                       "Authorization": f"Bearer {key}",
                       "User-Agent": "AnkiWordAdder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            _groq_limiter.update(r.headers)
            return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        if strict and e.code == 429:
            raise _AddonRateLimited(_parse_retry_after(e.headers))
        return ""
    except Exception:
        return ""


class _AddonRateLimited(Exception):
    """Raised by _groq_chat(strict=True) on HTTP 429 — used to end a burst in 批次回填.
    retry_after = seconds to wait before retrying (from Retry-After header, else 60)."""
    def __init__(self, retry_after=60):
        super().__init__("rate limited")
        self.retry_after = retry_after


def _parse_retry_after(headers, default=60):
    """Seconds to wait from a 429 Retry-After header; default if missing/invalid."""
    raw = headers.get("Retry-After") if headers else None
    try:
        secs = int(float(raw))
        return secs if secs > 0 else default
    except (TypeError, ValueError):
        return default


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

            sentence, engine = self._llm_sentence(word, self.association)
            if not sentence:
                sentence = f"Please add an example sentence for '{word}'."
            self.step.emit("sentence", "ok" if not any(p in sentence for p in PLACEHOLDERS) else "warn")

            # Image, Translation and Audio in parallel
            image_result = [None]
            translation_result = [""]
            sentence_cn_result = [""]
            audio_filename = f"{word}_tts.mp3"
            front_audio_filename = f"{word}_word.mp3"

            def do_image():
                image_result[0] = self._fetch_image(word, definition=self.association, sentence=sentence)

            def do_translate():
                translation_result[0] = self._groq_translate(word, sentence)
                sentence_cn_result[0] = self._groq_translate_sentence(sentence)

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
            self.step.emit("sentence_cn", "ok" if sentence_cn_result[0] else "warn")
            image_field = image_result[0]

            self.finished.emit({
                "word":        word,
                "association": self.association,
                "sentence":    sentence,
                "image_field": image_field,
                "translation": translation_result[0],
                "sentence_cn": sentence_cn_result[0],
                "audio_filename": audio_filename,
                "front_audio_filename": front_audio_filename,
            })
        except Exception as e:
            self.error.emit(str(e))

    # ── helpers ──────────────────────────────────────────────────────────────

    def _groq_sentence(self, word, association=""):
        return _groq_chat(_sentence_prompt(word, association), temperature=0.7, max_tokens=200, timeout=15)

    def _llm_sentence(self, word, association=""):
        result = self._groq_sentence(word, association)
        if result and len(result) > 10:
            return result, "Groq"
        return "", "failed"        # Groq 失敗就回空 → 上層退 placeholder，等下次補（不再走地端）

    def _groq_translate(self, word, sentence):
        """Traditional Chinese meaning of word AS USED IN the sentence ('' on failure).
        Proper nouns (frameworks/products) stay in English."""
        prompt = (f'Give the Traditional Chinese meaning of "{word}" as it is used in this '
                  f'sentence: "{sentence}". Give ONE concise translation only — do NOT list '
                  f'synonyms or near-duplicate terms (e.g. never "水杯、茶杯"). If "{word}" is a '
                  f'product / framework / library / tool proper noun (e.g. Spring, React, Docker, '
                  f'Hazelcast), do NOT translate it — output the English name as-is. Keep it short '
                  f'(usually 1-4 characters; a little longer only if a single term genuinely needs '
                  f'it). Output only the Chinese, or for a proper noun the English name, no explanation.')
        reply = _groq_chat(prompt, temperature=0.3, max_tokens=32, timeout=10)
        return _accept_word_translation(word, reply)

    def _groq_translate_sentence(self, sentence, *, strict=False):
        """Traditional Chinese translation of a full sentence. '' on failure.
        strict=True raises _AddonRateLimited on 429 (for the 批次回填 burst engine)."""
        if not sentence:
            return ""
        prompt = ('Translate this English sentence into natural, complete Traditional '
                  'Chinese. Keep product / framework / library / tool proper nouns (e.g. '
                  'Spring, React, Hazelcast) in English inside the translation; do not '
                  'translate such names literally. Output only the translation. No explanation, '
                  f'no quotes.\n\nSentence: "{sentence}"')
        reply = _groq_chat(prompt, temperature=0.3, max_tokens=200, timeout=15,
                           strict=strict).strip().strip('"').strip()
        if not re.search(r"[一-鿿]", reply):              # no Chinese → fail
            return ""
        if len(re.findall(r"[A-Za-z]{2,}", reply)) >= 3:  # 3+ English words = preamble; keep a single embedded term
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
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
        except Exception:               # timeout / spawn failure → no image; leave blank for ⌘S to retry
            return ""                   # runs in do_image thread; must not raise or it crashes the thread
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
        self.setMinimumWidth(580)        # wider than the 5-box row so the stretches centre it (side margins)
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

        self.add_btn.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._set_status(f"Generating: {word}")
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
            if "Sentence_CN" in note:
                note["Sentence_CN"] = data.get("sentence_cn", "")

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
    """One card's progress: word + Sentence/Audio/Image/Meaning/Translation boxes + 'added!' badge.
    Fields already present start green; missing ones start grey and flip on completion."""

    def __init__(self, word, present, parent=None):
        super().__init__(parent)
        self.word = word
        self._boxes = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        self.checkbox = QCheckBox()       # left-most: pick which cards to complete (default unchecked)
        lay.addWidget(self.checkbox)
        wl = QLabel(word)
        wl.setMinimumWidth(120)
        wl.setStyleSheet("font-weight:600; color:#1E293B;")
        lay.addWidget(wl)
        for key, _label in BACKFILL_BOXES:    # all five fields, incl. the sentence translation
            box = QLabel()
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.setFixedWidth(90)         # uniform box width regardless of label length
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

    def is_checked(self):
        return self.checkbox.isChecked()


# ── backfill worker ───────────────────────────────────────────────────────────

MAX_BACKFILL_WORKERS = 3


class BackfillWorker(QThread):
    step      = pyqtSignal(object, str, str)   # (note_id, field, ...) — object: note ids exceed 32-bit int
    card_done = pyqtSignal(object)             # (note_id) finished successfully
    finished  = pyqtSignal(list)
    error     = pyqtSignal(str)

    def __init__(self, notes, media_dir):
        super().__init__()
        self.notes     = notes
        self.media_dir = media_dir
        self._w = Worker.__new__(Worker)
        self._w.media_dir = media_dir
        self._hit_limit = False        # hit a real rate-limit wall → stop the rest, dialog notifies
        self.retry_after = 0

    def _process_one(self, note):
        note_id = note["noteId"]
        word = _clean_text(note["fields"]["Front"]["value"], lower=True)
        # Deterministic rate-limit gate: near the cloud limit → stop here and skip the rest
        # immediately (the dialog says how many are left). No waiting.
        if self._hit_limit:
            return f"skip {word}"
        wall = _groq_limiter.wall_secs()
        if wall > 0:
            self._hit_limit = True
            self.retry_after = max(self.retry_after, int(wall) + 1)
            return f"skip {word}"
        fields = {}

        current = note["fields"]["Sentence"]["value"]
        if not current or any(p in current for p in PLACEHOLDERS):
            assoc = _clean_text(note["fields"].get("Association", {}).get("value", ""))
            sentence, _ = self._w._llm_sentence(word, assoc)
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
        need_sentence_cn = not note["fields"].get("Sentence_CN", {}).get("value", "")

        image_result = [None]
        translation_result = [""]
        sentence_cn_result = [""]
        img_thread = trans_thread = None

        if need_translation or need_sentence_cn:
            def do_translate(w=word, s=sentence):     # both Groq text calls share one thread
                if need_translation:
                    translation_result[0] = self._w._groq_translate(w, s)
                if need_sentence_cn:
                    sentence_cn_result[0] = self._w._groq_translate_sentence(s)
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
        if need_sentence_cn:
            if sentence_cn_result[0]:
                fields["Sentence_CN"] = sentence_cn_result[0]
            self.step.emit(note_id, "sentence_cn", "ok" if sentence_cn_result[0] else "warn")

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


REFILL_CLEAR_FIELDS = ["Sentence", "Sentence_CN", "Image_Prompt",
                       "Audio", "Front_Audio", "Translation"]


# ── backfill dialog ───────────────────────────────────────────────────────────

class BackfillDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Complete Missing Cards")
        self.setMinimumWidth(820)        # room for word col + 5 boxes + the '…added!' badge
        self.setMinimumHeight(380)
        self._worker = None
        self._rows = {}
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Cards missing Sentence / Audio / Image / Meaning / Translation:"))

        self.select_all = QCheckBox("Select all")
        self.select_all.stateChanged.connect(self._on_select_all)
        root.addWidget(self.select_all)

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
        self.run_btn = QPushButton("Complete Selected (0)")
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
            sentence_cn = note["Sentence_CN"] if "Sentence_CN" in note else ""
            has_img = "<img" in note["Image_Prompt"]
            audio_ok = bool(note["Audio"]) and bool(front_audio)
            incomplete = (not note["Sentence"] or bad_sentence or not note["Audio"]
                          or not has_img or not front_audio or not translation
                          or not sentence_cn)
            if not incomplete:
                continue

            word = _clean_text(note["Front"])
            if not _looks_english(word):       # not English → don't fill, just flag it
                invalid += 1
                lbl = QLabel(f"{word or note['Front']} (contains non-English characters, cannot be created)")
                lbl.setStyleSheet("color:#ea580c; padding:4px;")
                self._rows_box.addWidget(lbl)
                continue

            present = {
                "sentence": bool(note["Sentence"]) and not bad_sentence,
                "image": has_img,
                "audio": audio_ok,
                "translation": bool(translation),
                "sentence_cn": bool(sentence_cn),
            }
            row = FieldRow(word, present)
            row.checkbox.stateChanged.connect(lambda *_: self._update_selection())
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
                    "Sentence_CN":  {"value": sentence_cn},
                }
            })
        self._rows_box.addStretch()
        self._pending_notes = notes
        parts = []
        if notes:
            parts.append(f"{len(notes)} card(s) need filling.")
        if invalid:
            parts.append(f"{invalid} card(s) contain non-English characters and cannot be created (please fix or delete).")
        self.status.setText(" ".join(parts) if parts else "All cards are complete!")
        self.select_all.setEnabled(bool(notes))
        self._update_selection()

    def _update_selection(self):
        n = sum(1 for r in self._rows.values() if r.is_checked())
        self.run_btn.setText(f"Complete Selected ({n})")
        self.run_btn.setEnabled(n > 0)

    def _on_select_all(self, state):
        checked = self.select_all.isChecked()
        for row in self._rows.values():
            row.checkbox.setChecked(checked)

    def _on_run(self):
        selected = [n for n in self._pending_notes
                    if (r := self._rows.get(n["noteId"])) and r.is_checked()]
        if not selected:
            return
        self.run_btn.setEnabled(False)
        self.select_all.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._worker = BackfillWorker(selected, mw.col.media.dir())
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
        if getattr(self._worker, "_hit_limit", False):
            left = len(self._worker.notes) - ok
            secs = int(self._worker.retry_after)
            self.status.setText(f"Hit the cloud rate limit — completed {ok}, {left} still need "
                                f"filling. Try again in ~{secs}s, then reselect.")
        else:
            self.status.setText(f"Done — {ok} card(s) updated. Remember to sync Anki!")
        for row in self._rows.values():        # reset selection so the next batch starts clean
            row.checkbox.setChecked(False)
        self.select_all.setChecked(False)
        self.select_all.setEnabled(True)
        self._update_selection()


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
        root.addWidget(QLabel("Duplicate cards with the same Front after normalization. Check the ones to delete (keep at least one per group):"))
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Word / Card", "Sentence"])
        self.tree.setColumnWidth(0, 220)
        root.addWidget(self.tree)

        self.status = QLabel("")
        root.addWidget(self.status)

        btns = QHBoxLayout()
        self.del_btn = QPushButton("Delete Selected")
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
            parent = QTreeWidgetItem(self.tree, [f"{key}  ({len(items)} cards)", ""])
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
            self.status.setText(f"Found {len(dup_groups)} duplicate group(s), {total} card(s) total.")
            self.del_btn.setEnabled(True)
        else:
            self.status.setText("No duplicate cards.")
            self.del_btn.setEnabled(False)

    def _on_delete(self):
        to_delete = []
        for i in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(i)
            checked = [parent.child(j).data(0, Qt.ItemDataRole.UserRole)
                       for j in range(parent.childCount())
                       if parent.child(j).checkState(0) == Qt.CheckState.Checked]
            if checked and len(checked) == parent.childCount():
                self.status.setText(f"All cards in '{parent.text(0)}' are checked; keep at least one per group.")
                return
            to_delete.extend(checked)

        if not to_delete:
            self.status.setText("No cards selected.")
            return

        reply = QMessageBox.question(
            self, "Confirm Deletion",
            f"Delete the {len(to_delete)} selected card(s)? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        mw.col.remove_notes(to_delete)
        mw.col.save()
        mw.reset()
        self._scan()
        self.status.setText(f"Deleted {len(to_delete)} card(s). Remember to sync Anki!")


# ── 批次回填整句翻譯（Sentence_CN）—— burst 引擎 + 時間盒選單 ───────────────────

SENTENCE_CN_RPM = 25          # 約略每分鐘筆數（Groq 12000 token/分 ÷ ~480/句 ≈ 25）；僅用於預估顯示
# (label, budget_seconds | None=直接完成)
SENTENCE_CN_MODES = [("1 min", 60), ("2 min", 120), ("5 min", 300),
                     ("10 min", 600), ("Run to completion", None)]


class SentenceCNWorker(QThread):
    """Paced translator: translate continuously; on 429 wait Retry-After (~the token
    refill, a few seconds) then carry on. Runs until the time budget is spent
    (None = run until everything is done). Honours the chosen seconds — a 30s job
    spends ~30s translating as fast as the rate allows."""
    LONG_BLOCK = 130                            # Retry-After above this = hard block (e.g. daily quota)
    progress = pyqtSignal(int, int, int)        # (done, total, remaining_secs; -1 = 直接完成)
    waiting  = pyqtSignal(int, int, int)        # (seconds_left, done, total)
    finished = pyqtSignal(int, int)             # (done, remaining)

    def __init__(self, notes, budget_seconds):
        super().__init__()
        self.notes = notes              # [{"noteId":…, "sentence":…}] pre-fetched, missing Sentence_CN
        self.budget = budget_seconds    # None = 直接完成
        self._w = Worker.__new__(Worker)
        self._stop = False
        self.blocked_secs = 0           # set if we stop because of a long (daily) block

    def stop(self):
        self._stop = True

    def _remaining(self, start):
        import time
        if self.budget is None:
            return -1
        return max(0, int(self.budget - (time.monotonic() - start)))

    def run(self):
        import time
        total = len(self.notes)
        done = 0
        i = 0
        start = time.monotonic()
        try:
            while i < total and not self._stop:
                if self.budget is not None and time.monotonic() - start >= self.budget:
                    break
                note = self.notes[i]
                try:
                    cn = self._w._groq_translate_sentence(note["sentence"], strict=True)
                except _AddonRateLimited as e:
                    wait = e.retry_after
                    elapsed = time.monotonic() - start
                    if self.budget is not None and elapsed + wait > self.budget:
                        break                    # no time left to wait out the cooldown
                    if wait > self.LONG_BLOCK:
                        self.blocked_secs = wait  # daily / long quota → stop and report
                        break
                    target = time.monotonic() + wait
                    while not self._stop:        # wait out the refill, then retry SAME note
                        left = target - time.monotonic()
                        if left <= 0:
                            break
                        self.waiting.emit(int(left) + 1, done, total)
                        time.sleep(0.3)
                    continue
                if cn:
                    try:
                        self._update(note["noteId"], cn)
                        done += 1
                    except Exception:
                        pass   # write failed (AnkiConnect hiccup) → leave unfilled, re-picked next run
                i += 1
                self.progress.emit(done, total, self._remaining(start))
        finally:
            self.finished.emit(done, total - done)

    def _update(self, note_id, cn):
        payload = json.dumps({
            "action": "updateNoteFields", "version": 6,
            "params": {"note": {"id": note_id, "fields": {"Sentence_CN": cn}}}
        }).encode()
        with urllib.request.urlopen(
            urllib.request.Request(ANKI_URL, data=payload,
                        headers={"Content-Type": "application/json"}),
            timeout=15,
        ) as resp:
            err = json.loads(resp.read().decode()).get("error")
        if err:
            raise RuntimeError(f"AnkiConnect: {err}")


def _hline():
    """Horizontal separator line between panel sections."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _section_title(text):
    lbl = QLabel(f"▸ {text}")
    lbl.setStyleSheet("font-weight:700; font-size:14px; color:#1E293B; padding-top:4px;")
    return lbl


class TranslateSection(QWidget):
    """Top section of Batch Operations: bulk-fill Sentence_CN with an up-front time
    estimate and a time-box menu. Paced by SentenceCNWorker; resume is automatic
    (each scan re-checks what's missing)."""

    def __init__(self, parent=None):
        super().__init__(parent)
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
            notes.append({"noteId": nid, "sentence": sentence})
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
        self.progress_bar.setVisible(False)
        self.stop_btn.setEnabled(False)
        blocked = getattr(self._worker, "blocked_secs", 0)
        if blocked:
            self.status.setText(
                f"Translated {done}. Hit Groq's longer rate limit (need to wait ~{blocked}s, "
                f"possibly the daily quota); please come back later. {remaining} left.")
        else:
            self.status.setText(f"Translated {done} this run, {remaining} left. Remember to sync Anki!")
        self._scan()       # refresh count + re-enable mode buttons for another round


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
        for item in self._flagged:
            note = mw.col.get_note(item["nid"])
            for f in REFILL_CLEAR_FIELDS:        # keep Front + Association, blank the rest
                if f in note:
                    note[f] = ""
            mw.col.update_note(note)
            mw.col.set_user_flag_for_cards(0, item["cids"])
        mw.col.save()
        mw.reset()
        n = len(self._flagged)
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


class BatchOperationsDialog(QDialog):
    """Unified batch panel: sentence-translation backfill on top, clear-flagged below,
    separated by a divider. Built from stacked self-contained section widgets so more
    batch operations can be added later as new blocks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Batch Operations")
        self.setMinimumWidth(640)
        root = QVBoxLayout(self)
        root.addWidget(TranslateSection(self))
        root.addWidget(_hline())
        root.addWidget(ClearFlaggedSection(self, parent=self))
        root.addWidget(_hline())

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)


# ── menu entries ──────────────────────────────────────────────────────────────

def open_dialog():
    AddWordDialog(mw).exec()

def open_backfill_dialog():
    BackfillDialog(mw).exec()

def open_duplicates_dialog():
    FindDuplicatesDialog(mw).exec()

def open_batch_operations_dialog():
    BatchOperationsDialog(mw).exec()

DEFAULT_SHORTCUTS = {"add": "Ctrl+A", "complete": "Ctrl+S", "find_duplicates": "Ctrl+D",
                     "backfill_cn": "Ctrl+F"}
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
        ("backfill_cn", "Batch Operations"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("My Word Adder — Shortcuts")
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
        cfg = mw.addonManager.getConfig(__name__) or {}
        sc = cfg.setdefault("shortcuts", {})
        sc.update(new)
        mw.addonManager.writeConfig(__name__, cfg)
        for key, act in ACTIONS.items():          # apply live — no restart needed
            act.setShortcut(QKeySequence(sc.get(key, DEFAULT_SHORTCUTS[key])))
        tooltip("Shortcuts updated", period=2000)
        self.accept()


def open_settings_dialog():
    SettingsDialog(mw).exec()


_add_menu_action("Add English Word…", "add", open_dialog)
_add_menu_action("Complete Missing Cards…", "complete", open_backfill_dialog)
_add_menu_action("Find Duplicate Words…", "find_duplicates", open_duplicates_dialog)
_add_menu_action("Batch Operations…", "backfill_cn", open_batch_operations_dialog)

_settings_action = QAction("My Word Adder Settings…", mw)
_settings_action.triggered.connect(open_settings_dialog)
mw.form.menuTools.addAction(_settings_action)
