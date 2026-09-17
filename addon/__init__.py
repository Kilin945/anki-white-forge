"""
My Word Adder — add English words to My Daily English with auto-fill.
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

import aqt
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

from . import _llm_dispatch as _lld

# 雙 provider 分流(KEEP-IN-SYNC 鏡像;無 .gemini_key 自動退化為單 Groq)
_dispatcher = _lld.Dispatcher(
    [p for p in (_lld.GroqProvider.load(), _lld.GeminiProvider.load()) if p])
_log = _lld.get_logger()   # 批次/LLM 事件集中記錄到 logs/addon_llm.log（gitignored）

DECK_NAME    = "My Daily English"
MODEL_NAME   = "English_White_Method"
ANKI_URL     = "http://127.0.0.1:8765"
PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]
# repo 根從自己的位置推 — addon 是 symlink 掛進 Anki 的 addons21,
# 所以要 realpath 才會落在 repo 而不是 symlink 所在的資料夾。
_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
VENV_PYTHON     = os.path.join(_REPO, ".venv", "bin", "python")
GTTS_SCRIPT     = os.path.join(_REPO, "_gtts_helper.py")
IMAGE_SCRIPT    = os.path.join(_REPO, "_image_helper.py")
VALIDATE_SCRIPT = os.path.join(_REPO, "_validate_helper.py")
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


def _clean_text(raw, *, lower=False):
    # strip only real HTML tags (`<tag ...>` / `</tag>`); leave literal `<`…`>` in content
    text = html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", raw)).replace("\xa0", " ").strip()
    return text.lower() if lower else text


ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\- ]*")


def _looks_english(word):
    """True if plausibly English (letters + space / - / '); rejects CJK, digits, symbols.
    Shared charset gate for both ⌘D (add) and ⌘S (complete)."""
    return bool(ENGLISH_WORD_RE.fullmatch((word or "").strip()))


def _sentence_word_count(html_value):
    """例句欄 → 英文字數(去 HTML)。空句/佔位符回 0 — 那是「缺句」(⌘S 的事),
    不是「長句」,回 0 讓它永遠不超過門檻。"""
    text = _clean_text(html_value or "")
    if not text or any(p in text for p in PLACEHOLDERS) or \
            text.startswith("Please add an example sentence"):
        return 0
    return len(text.split())


def _clamp_length_threshold(raw, default=20):
    """Rebuild Long Sentences 的門檻輸入解析:空白/非數字→default,下限 1,無上限
    (填 999 掃不到東西是合理結果)。"""
    try:
        n = int(str(raw).strip())
    except (ValueError, TypeError):
        return default
    return max(1, n)


def _long_sentence_label(word, count):
    """清單項目樣式:transient(27)。"""
    return f"{word}({count})"


def _sentence_usable(sentence):
    """句子可不可以餵給依賴句意的下游（翻譯/搜圖/配音）。
    空句或佔位符 → False：下游全部跳過，等真句子生出來一起重做，
    避免「翻譯了佔位符」這類欄位彼此不一致的髒卡。
    KEEP-IN-SYNC: core/text.py::sentence_usable（addon 不能 import core）。"""
    return bool(sentence) and not any(p in sentence for p in PLACEHOLDERS)


def _sentence_to_write(current, generated, word):
    """生成結果 → 該寫入 Sentence 的值；None = 不要寫（保住既有真句子）。
    - 生成成功 → 寫生成句
    - 生成失敗且既有值是空/佔位符 → 寫佔位符（維持原行為，讓卡片仍被掃到）
    - 生成失敗但既有值是真句子 → None（絕不用佔位符蓋掉真句子）
    第二次補卡的資料損毀事故（撞限失敗仍用佔位符蓋掉已生成的真句子）就是少了這一層守門；
    第一層防線是 `_on_run` 改讀最新欄位，這裡是即使欄位判斷有誤也絕不覆蓋真句子的第二層。"""
    if generated:
        return generated
    if not current or any(p in current for p in PLACEHOLDERS):
        return f"Please add an example sentence for '{word}'."
    return None


def _need_sentence_audio(existing_audio, sentence, sentence_was_rewritten):
    """句音要不要(重)生成:佔位符絕不配音(留空,等真句配對生成);
    句子這一輪被重寫 → 強制重生(舊音檔已不匹配);否則缺才補。"""
    if not sentence or any(p in sentence for p in PLACEHOLDERS) or \
            sentence.startswith("Please add an example sentence"):
        return False
    return (not existing_audio) or sentence_was_rewritten


# traceback 濃縮:整頁 stderr → 最關鍵的 1~2 行例外訊息。
# Python traceback 精華永遠在最下面,且例外行「頂格不縮排」;
# File/code/^^^^ 都縮排、框架句可辨識 → 過濾掉後取最後幾行即為病根。
_TB_FRAME_LINES = (
    "Traceback (most recent call last):",
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
)


def _key_error_lines(stderr, max_lines=2):
    """把 subprocess 的整頁 traceback 濃縮成最關鍵的例外行(給面板顯示)。
    過濾縮排的 File/code 行、^^^^/~~~~ 指示箭頭、...<N lines>... 省略標記、
    框架句;保留頂格的例外行(如 socket.gaierror: ...),取最後 max_lines 行。
    萃取不到(非 traceback 字串)→ 回最後一條非空行(截斷)。"""
    lines = [ln.rstrip() for ln in (stderr or "").splitlines()]
    exc_lines = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if ln[0].isspace():            # 縮排 → File / code / ^^^^ / ~~~~
            continue
        if set(s) <= set("^~"):        # 純指示箭頭(保險:萬一沒縮排)
            continue
        if s.startswith("...<") and s.endswith(">..."):   # ...<5 lines>...
            continue
        if s in _TB_FRAME_LINES:
            continue
        exc_lines.append(s)
    if exc_lines:
        return "\n".join(exc_lines[-max_lines:])
    tail = [ln.strip() for ln in lines if ln.strip()]     # fallback:最後一條非空行
    return tail[-1][:300] if tail else ""


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


def _groq_chat(prompt, *, temperature, max_tokens, timeout, strict=False, effort="low"):
    """One LLM text call via the dual-provider dispatcher; '' on no key / failure.
    strict=True surfaces both-providers-limited as _AddonRateLimited (so the burst
    engine can pace/stop) instead of swallowing it as ''."""
    if not _dispatcher.providers:
        return ""
    try:
        return _dispatcher.generate(prompt, temperature=temperature,
                                    max_tokens=max_tokens, timeout=timeout,
                                    effort=effort)
    except _lld.AllProvidersLimited as e:
        if strict:
            raise _AddonRateLimited(int(e.soonest_reset) + 1)
        return ""
    except Exception:
        return ""


class _AddonRateLimited(Exception):
    """Raised by _groq_chat(strict=True) when all providers are rate-limited — used to
    end a burst in 批次回填. retry_after = seconds to wait before retrying."""
    def __init__(self, retry_after=60):
        super().__init__("rate limited")
        self.retry_after = retry_after


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
            sentence_ok = _sentence_usable(sentence)
            self.step.emit("sentence", "ok" if sentence_ok else "warn")

            # Image, Translation and Audio in parallel — but only when the sentence is
            # usable: 依賴句意的下游（搜圖/翻譯/句音）拿佔位符當輸入會做出彼此不一致
            # 的髒卡（例：Sentence_CN 是佔位符的翻譯）→ 全部跳過亮橘，之後 ⌘S 連同
            # 句子一起重做。Front_Audio（單字音）與句子無關，照做。
            image_result = [None]
            translation_result = [""]
            sentence_cn_result = [""]
            audio_filename = f"{word}_tts.mp3" if sentence_ok else ""
            front_audio_filename = f"{word}_word.mp3"

            def do_image():
                image_result[0] = self._fetch_image(word, definition=self.association, sentence=sentence)

            def do_translate():
                translation_result[0] = self._groq_translate(word, sentence)
                sentence_cn_result[0] = self._groq_translate_sentence(sentence)

            img_thread = trans_thread = None
            if sentence_ok:
                img_thread = threading.Thread(target=do_image)
                trans_thread = threading.Thread(target=do_translate)
                img_thread.start()
                trans_thread.start()

            audio_items = [
                {"text": word, "filepath": os.path.join(self.media_dir, front_audio_filename), "voice": VOICE_WORD},
            ]
            if sentence_ok:
                audio_items.append(
                    {"text": sentence, "filepath": os.path.join(self.media_dir, audio_filename), "voice": VOICE_SENTENCE})
            try:
                self._make_audio_batch(audio_items)
                self.step.emit("audio", "ok" if sentence_ok else "warn")
            finally:
                if img_thread:
                    img_thread.join()      # always join so threads don't leak on audio failure
                if trans_thread:
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
        # 造句是多條件約束任務 → 較高思考等級（其餘呼叫維持預設 low）
        return _groq_chat(_sentence_prompt(word, association), temperature=0.7,
                          max_tokens=200, timeout=15, effort="medium")

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
            detail = _key_error_lines(result.stderr)
            raise RuntimeError(
                "Audio generation failed. Check your network and retry."
                + (f"\n\n{detail}" if detail else "")
            )


# ── 非阻塞視窗的三道防線 ──────────────────────────────────────────────────────
# 批次視窗改用非阻塞 show()（不鎖 Anki）之後，exec() 原本隱性提供的保護沒了，
# 這裡逐一補回：批次互斥、關窗不留殭屍 worker、卡片被別處刪掉的防呆。

_batch_owner = None                  # 目前在跑批次的視窗名稱（None = 沒有）
_batch_lock = threading.Lock()


def _batch_acquire(label):
    """取得批次權；別的視窗正在跑就回 False（呼叫端負責告知使用者）。
    為何要互斥：兩個批次同時跑會搶同一份速率額度，而且 ⌘S 與 ⌘F 都會寫
    Sentence_CN → 對同一批卡的同一欄位重複寫、互相蓋掉。"""
    global _batch_owner
    with _batch_lock:
        if _batch_owner is not None and _batch_owner != label:
            return False
        _batch_owner = label
        return True


def _batch_release(label):
    """釋放批次權；只有持有者能釋放（別的視窗收尾不該解掉別人的鎖）。"""
    global _batch_owner
    with _batch_lock:
        if _batch_owner == label:
            _batch_owner = None


def _batch_busy():
    return _batch_owner


def _batch_busy_message():
    return (f"A batch is already running in {_batch_owner}. "
            f"Wait for it to finish, then try again.")


def _blocked_by_batch(show_message):
    """跑批中不准動卡片：清空欄位 / 建卡 / 刪卡會和正在寫同一批卡的 worker 打架。
    擋下來就回 True（並用傳進來的函式把原因顯示給使用者）。"""
    if _batch_busy() is None:
        return False
    show_message(_batch_busy_message())
    return True


def _live_note(note_id):
    """note 還在就回它，已被刪掉回 None。
    非阻塞視窗開著時卡片可能被別處刪掉（⌘F Clean Test Cards、⌘D 刪重複、Browse），
    `mw.col.get_note()` 會拋 NotFoundError → 呼叫端跳過，而不是讓例外冒到 Anki。"""
    try:
        return mw.col.get_note(note_id)
    except Exception:
        return None


class _BatchDialogMixin:
    """批次視窗共用：Anki dialog-manager 契約 + 跑批中不關窗。

    - `done()`（Close / X / accept 都會走到）在批次跑到一半時只「請 worker 停」，
      不真的關窗——視窗一關，worker 的 signal 就打到已刪除的 Qt 物件，而且
      `_on_finished` 還會在背後 `mw.col.save()` / `mw.reset()`。worker 收尾時
      各 dialog 呼叫 `_end_batch()`，那時才真的關。
    - `closeWithCallback()`：Anki 退出 / 切 profile 時 `aqt.dialogs.closeAll` 會呼叫，
      先停批並等 thread 真的結束，才放 Anki 繼續卸載 collection。
    """
    _DM_NAME = None          # aqt.dialogs 註冊名（子類覆寫）
    _BATCH_LABEL = None      # 批次互斥用的顯示名（子類覆寫）
    # 退出時等 worker 收尾的上限。取 15s = 單次 LLM 呼叫的 timeout：手上那張卡的
    # 網路呼叫最多就這麼久，等掉它才不會對正在卸載的 collection 寫入。有界 → 不會
    # 無限卡住 Anki 關閉。
    _STOP_WAIT_MS = 15000
    _CLOSE_WAIT_MS = 3000    # 收尾後關窗前的等待上限（見 _force_close）

    def _active_worker(self):
        """回目前在跑的 worker，沒有就 None。
        worker 不掛在 self 上的視窗要覆寫（Batch Operations 的掛在 section 上）。"""
        w = getattr(self, "_worker", None)
        return w if (w is not None and w.isRunning()) else None

    def _batch_active(self):
        return self._active_worker() is not None

    def _request_stop(self):
        w = self._active_worker()
        if w is not None and hasattr(w, "stop"):
            w.stop()

    def _set_batch_status(self, text):
        """顯示「正在停批」訊息；沒有 status 標籤的視窗覆寫這個。"""
        status = getattr(self, "status", None)
        if status is not None:
            status.setText(text)

    def done(self, r):
        if self._batch_active():
            self._close_pending = True
            self._close_result = r
            self._request_stop()
            self._set_batch_status(
                "Stopping the batch — this window closes when the current card is done.")
            return
        aqt.dialogs.markClosed(self._DM_NAME)
        super().done(r)

    def _end_batch(self):
        """worker 收尾時由各 dialog 的 _on_finished / _on_error 呼叫：
        釋放批次權，若使用者在跑批中按過 Close 就補上真正的關窗。"""
        _batch_release(self._BATCH_LABEL)
        if getattr(self, "_close_pending", False):
            self._close_pending = False
            self._force_close(getattr(self, "_close_result", 0))

    def _force_close(self, r):
        """真的關窗。不走 done() 的守門：worker 用的是自訂 finished signal，
        在 run() 還沒返回時就發出 → 這一刻 isRunning() 仍是 True，走 done()
        會被守門擋掉、視窗永遠關不掉。收尾 signal 既然已發完，剩下的只是 thread
        退出，等一下即可（順帶避免 GC 掉還在跑的 QThread）。"""
        w = self._active_worker()
        if w is not None:
            w.wait(self._CLOSE_WAIT_MS)
        aqt.dialogs.markClosed(self._DM_NAME)
        self._close_pending = False        # 與 closeWithCallback 一致:關掉了就不留旗標
        super().done(r)

    def closeWithCallback(self, callback):
        w = self._active_worker()
        if w is not None:
            self._request_stop()
            w.wait(self._STOP_WAIT_MS)     # 有界等待:worker 真的結束才放 Anki 卸載 collection
        _batch_release(self._BATCH_LABEL)
        self._close_pending = False
        try:
            self._force_close(0)
        finally:
            callback()


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

        if not _batch_acquire(self._BATCH_LABEL):     # 別的批次在跑 → 會互搶額度
            self._set_status(_batch_busy_message(), "warn")
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
            self._end_batch()

    def _on_error(self, msg):
        self._set_status(f"Error: {msg}", "warn")
        self.add_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self._end_batch()


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
SHORT_WALL_WAIT = 2.0      # 秒。牆比這短就原地等掉續跑；更長才停批回報(人工核可的政策)


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
        self._stopped = False          # 使用者關窗/Anki 退出 → 做完手上這張就收工
        self.retry_after = 0
        self.limit_resets = {}         # 撞牆當下兩家的恢復秒數(對話框訊息用)

    def stop(self):
        """請 worker 收工：剩下的卡直接跳過（同 _hit_limit 的既有跳過機制）。"""
        self._stopped = True

    def _process_one(self, note):
        note_id = note["noteId"]
        word = _clean_text(note["fields"]["Front"]["value"], lower=True)
        # Deterministic rate-limit gate: near the cloud limit → stop here and skip the rest
        # immediately (the dialog says how many are left). No waiting.
        if self._hit_limit or self._stopped:
            return f"skip {word}"
        wall = _dispatcher.wall_secs()
        if 0 < wall <= SHORT_WALL_WAIT:
            _log.info("short wall %.1fs — waiting", wall)
            time.sleep(wall + 0.2)             # 短牆:等掉它,額度窗口一過就續跑
            wall = _dispatcher.wall_secs()
        if wall > 0:
            self._hit_limit = True
            self.retry_after = max(self.retry_after, int(wall) + 1)
            self.limit_resets = _dispatcher.resets()
            _log.warning("⌘S stopped: resets=%s", self.limit_resets)
            return f"skip {word}"
        fields = {}

        current = note["fields"]["Sentence"]["value"]
        if not current or any(p in current for p in PLACEHOLDERS):
            assoc = _clean_text(note["fields"].get("Association", {}).get("value", ""))
            sentence, _ = self._w._llm_sentence(word, assoc)
            to_write = _sentence_to_write(current, sentence, word)
            if to_write is not None:
                fields["Sentence"] = to_write
                sentence = to_write
                self.step.emit(note_id, "sentence",
                               "ok" if not any(p in to_write for p in PLACEHOLDERS) else "warn")
            else:
                sentence = _clean_text(current)   # keep the real sentence — don't overwrite with a placeholder
                self.step.emit(note_id, "sentence", "warn")
                _log.warning("%s: sentence gen failed, kept existing sentence", word)
        else:
            sentence = _clean_text(current)

        import threading
        need_image = "<img" not in note["fields"]["Image_Prompt"]["value"]
        need_audio = _need_sentence_audio(note["fields"]["Audio"]["value"],
                                          sentence, "Sentence" in fields)
        need_front = not note["fields"].get("Front_Audio", {}).get("value", "")
        need_translation = not note["fields"].get("Translation", {}).get("value", "")
        need_sentence_cn = not note["fields"].get("Sentence_CN", {}).get("value", "")

        # 句子不可用（生成失敗）→ 依賴句意的下游全部跳過亮橘，下次 ⌘S 連同句子一起
        # 重做——避免翻譯/搜圖拿佔位符當輸入的髒卡（句音由 _need_sentence_audio 自擋，
        # Front_Audio 與句子無關照做）
        if not _sentence_usable(sentence):
            for key, needed in (("image", need_image),
                                ("translation", need_translation),
                                ("sentence_cn", need_sentence_cn)):
                if needed:
                    self.step.emit(note_id, key, "warn")
            need_image = need_translation = need_sentence_cn = False

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
        _log.info("⌘S run start: %d cards", len(self.notes))
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

# Rebuild Long Sentences 清除的欄位 — 換句=全重建:句子三欄(Audio 是句子語音)之外,
# 連 Translation(依句中用法翻,換句可能換義)與 Image_Prompt(依句意搜的圖)也一起清;
# 只保留 Front/Association/Front_Audio(單字發音與句子無關)。使用者實測後定案。
REBUILD_CLEAR_FIELDS = ["Sentence", "Sentence_CN", "Audio",
                        "Translation", "Image_Prompt"]


def _note_incomplete(note):
    """True if the note is still missing ANY auto-filled field (Sentence / Audio /
    Image / Meaning / Translation). Single source of 'is this card done' — used both by
    the scan and the post-run count, so a card still missing only the translation
    correctly counts as not done (not 'done because we wrote something')."""
    bad_sentence = any(p in note["Sentence"] for p in PLACEHOLDERS)
    front_audio = note["Front_Audio"] if "Front_Audio" in note else ""
    translation = note["Translation"] if "Translation" in note else ""
    sentence_cn = note["Sentence_CN"] if "Sentence_CN" in note else ""
    return (not note["Sentence"] or bad_sentence or not note["Audio"]
            or "<img" not in note["Image_Prompt"] or not front_audio
            or not translation or not sentence_cn)


def _note_snapshot(note):
    """Anki Note -> the field-dict shape BackfillWorker expects, read fresh from
    mw.col.get_note() right now (main thread only — collection access must stay off
    worker threads). Used by `_on_run` so a second Complete click always re-reads
    current field values instead of trusting a snapshot taken when the dialog opened
    (that staleness is what let a rate-limited retry overwrite a good sentence with a
    placeholder — see _sentence_to_write). Notes already complete are harmless to pass
    through: BackfillWorker's need_* checks skip everything for them."""
    front_audio = note["Front_Audio"] if "Front_Audio" in note else ""
    translation = note["Translation"] if "Translation" in note else ""
    sentence_cn = note["Sentence_CN"] if "Sentence_CN" in note else ""
    return {
        "noteId": note.id,
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
    }


# ── backfill dialog ───────────────────────────────────────────────────────────

def _drop_notes(pending_notes, remove_ids):
    """Return pending_notes minus the given note ids — a fresh list, input untouched.
    Pure decision behind Remove Selected (view-only removal; cards stay in Anki)."""
    remove_ids = set(remove_ids)
    return [n for n in pending_notes if n["noteId"] not in remove_ids]


def _removal_status(removed, remaining):
    """Status line after Remove Selected drops rows from the list (view only)."""
    if remaining:
        return (f"Removed {removed} from the list. "
                f"{remaining} still shown. Remember to sync Anki!")
    return "All cleared from the list. Remember to sync Anki!"


class BackfillDialog(_BatchDialogMixin, QDialog):
    _DM_NAME = "WhiteForgeBackfill"
    _BATCH_LABEL = "Complete Missing Cards"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Complete Missing Cards")
        self.setMinimumWidth(820)        # room for word col + 5 boxes + the '…added!' badge
        self.setMinimumHeight(380)
        self._worker = None
        self._rows = {}
        self._setup_ui()
        self._scan()

    def reopen(self):
        """單例被叫回前面時 Anki 的 dialog manager 會呼叫這裡 → 重掃一次。
        不重掃的話「⌘F 清空紅旗卡 → Open Complete Missing Cards」會看到清空前的
        舊清單，剛清空的卡不在裡面，一鍵重生等於沒作用。批次還在跑就不動
        （重掃會把正在更新的列表整片換掉）。"""
        if self._batch_active():
            return
        self._clear_rows()
        self.remove_btn.setVisible(False)
        self.select_all.setChecked(False)
        self._scan()

    def _clear_rows(self):
        """清掉列表所有 widget 與 stretch —— 重掃前必清,否則新舊清單疊加。"""
        self._rows = {}
        while self._rows_box.count():
            item = self._rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

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
        self.remove_btn = QPushButton("Remove Selected (0)")
        self.remove_btn.setEnabled(False)
        self.remove_btn.setVisible(False)     # only appears after a batch finishes
        self.remove_btn.clicked.connect(self._on_remove_selected)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(self.run_btn)
        btns.addWidget(self.remove_btn)
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
            if not _note_incomplete(note):
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
        # 預設全選:日常用法就是「開窗、按一下、全部補完」,要挑掉某張卡再自己取消勾選。
        # 直接呼叫 _on_select_all 而不是靠 setChecked 的 stateChanged —— 值沒變時
        # Qt 不會 emit,reopen 進來若 select_all 已是 True,新掃出來的列就會漏勾。
        self.select_all.setChecked(bool(notes))
        self._on_select_all()
        self._update_selection()

    def _update_selection(self):
        n = sum(1 for r in self._rows.values() if r.is_checked())
        self.run_btn.setText(f"Complete Selected ({n})")
        self.run_btn.setEnabled(n > 0)
        self.remove_btn.setText(f"Remove Selected ({n})")
        self.remove_btn.setEnabled(n > 0)

    def _on_select_all(self, state=None):
        checked = self.select_all.isChecked()
        for row in self._rows.values():
            row.checkbox.setChecked(checked)

    def _on_run(self):
        selected = [n for n in self._pending_notes
                    if (r := self._rows.get(n["noteId"])) and r.is_checked()]
        if not selected:
            return
        if not _batch_acquire(self._BATCH_LABEL):     # 別的批次在跑 → 會互搶額度/重複寫卡
            self.status.setText(_batch_busy_message())
            return
        # Re-read current field values now (not self._pending_notes, a snapshot from when
        # the dialog opened) — a second Complete click must not think fields are still
        # missing just because they were missing when the dialog was first opened.
        # 視窗非阻塞後卡片可能已被別處刪掉 → _live_note 跳過死掉的 id。
        fresh_notes = [_note_snapshot(note) for n in selected
                       if (note := _live_note(n["noteId"])) is not None]
        if not fresh_notes:
            _batch_release(self._BATCH_LABEL)
            self.status.setText("Those cards no longer exist — reopen this window to rescan.")
            return
        self.run_btn.setEnabled(False)
        self.select_all.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._worker = BackfillWorker(fresh_notes, mw.col.media.dir())
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
        # 已被刪掉的卡（非阻塞視窗開著時可能被別處刪除）兩邊都不算：不算「還缺」,
        # 也不算「已完成」——只從 total 扣掉。否則 3 張選取、跑到一半刪掉 1 張,
        # 會顯示「3 card(s) completed」而實際只做了 2 張。
        live = [note for n in self._worker.notes
                if (note := _live_note(n["noteId"])) is not None]
        total = len(live)
        left = sum(1 for note in live if _note_incomplete(note))
        done = total - left
        if getattr(self._worker, "_stopped", False):
            self.status.setText(f"Stopped — completed {done}, {left} still need filling.")
        elif getattr(self._worker, "_hit_limit", False):
            secs = int(self._worker.retry_after)
            resets = getattr(self._worker, "limit_resets", {})
            if len(resets) > 1:        # 雙 provider:報每家真實恢復時間
                self.status.setText(
                    f"Both providers out of quota — {_lld.format_reset_summary(resets)}. "
                    f"Completed {done}, {left} still need filling.")
            else:                      # 單 Groq 退化:沿用原措辭
                self.status.setText(
                    f"Hit the cloud rate limit — completed {done}, {left} still need "
                    f"filling. Try again in ~{secs}s, then reselect.")
        elif left:
            self.status.setText(f"Completed {done}, {left} still need filling — some fields "
                                f"didn't come back, try those again. Remember to sync Anki!")
        else:
            self.status.setText(f"Done — {done} card(s) completed. Remember to sync Anki!")
        # keep the boxes checked so the finished cards stay selected — the user then
        # clicks Remove Selected to clear them from the list (view only, cards stay in Anki)
        self.select_all.setEnabled(True)
        self.remove_btn.setVisible(True)
        self._update_selection()
        self._end_batch()

    def _on_remove_selected(self):
        """Drop the checked rows from this list — view only. The cards were just
        completed and stay in Anki; this only clears them off the dialog."""
        to_remove = [nid for nid, r in self._rows.items() if r.is_checked()]
        if not to_remove:
            return
        for nid in to_remove:                        # Qt side: drop the row widgets
            row = self._rows.pop(nid)
            self._rows_box.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._pending_notes = _drop_notes(self._pending_notes, to_remove)
        self.select_all.setChecked(False)
        self.status.setText(_removal_status(len(to_remove), len(self._rows)))
        if not self._rows:                           # list emptied → retire the button
            self.remove_btn.setVisible(False)
            self.select_all.setEnabled(False)
        self._update_selection()


# ── find duplicates dialog ─────────────────────────────────────────────────────

class FindDuplicatesDialog(_BatchDialogMixin, QDialog):
    """Find cards whose Front is the same after normalization (HTML/case-insensitive),
    and let the user pick which to delete. Catches dupes that slipped in via mobile."""

    _DM_NAME = "WhiteForgeDuplicates"
    _BATCH_LABEL = "Find Duplicate Words"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find Duplicate Words")
        self.setMinimumWidth(540)
        self.setMinimumHeight(420)
        self._setup_ui()
        self._scan()

    def reopen(self):
        self._scan()        # _scan() 自己會先 tree.clear()

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
        if _blocked_by_batch(self.status.setText):   # 別在批次寫卡時把卡刪掉
            return
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
        self._panel._end_batch()


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
            for f in REFILL_CLEAR_FIELDS:        # keep Front + Association, blank the rest
                if f in note:
                    note[f] = ""
            mw.col.update_note(note)
            mw.col.set_user_flag_for_cards(0, item["cids"])
            n += 1
        mw.col.save()
        mw.reset()
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


# Test-card helper — bare cards for manually testing the dialogs. KEEP IN SYNC with
# make_test_cards.py (CLI): same tag + same word list, so a card made by one tool is
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


class TestCardsSection(QWidget):
    """Batch Operations section: spawn / clean throwaway test cards (Front + Association
    only) so they show up in Complete Missing Cards for manual UI testing. Cards carry
    TEST_CARD_TAG so cleanup is one click. Synchronous — no worker/progress bar.
    UI-side twin of make_test_cards.py (CLI)."""

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


class BatchOperationsDialog(_BatchDialogMixin, QDialog):
    """Unified batch panel: sentence-translation backfill on top, clear-flagged
    in the upper-middle, rebuild long sentences in the lower-middle, test-card
    helper at the bottom, separated by dividers. Built from stacked self-contained
    section widgets so more batch operations can be added as new blocks."""

    _DM_NAME = "WhiteForgeBatchOps"
    _BATCH_LABEL = "Batch Operations"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Batch Operations")
        self.setMinimumSize(660, 760)      # 四個 section 疊起來已超過小視窗 → 給足高度

        # sections 放進可捲動的內容區:每個 section 保有自然高度、不再互相擠壓
        # (曾經四塊硬塞固定視窗 → 說明文字被裁、按鈕疊到清單上)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 0, 8, 0)   # 右緣留給捲軸
        self._translate = TranslateSection(self, parent=content)
        clear_flagged  = ClearFlaggedSection(self, parent=content)
        long_sentences = LongSentencesSection(self, parent=content)
        test_cards     = TestCardsSection(self, parent=content)
        self._sections = [self._translate, clear_flagged, long_sentences, test_cards]
        body.addWidget(self._translate)
        body.addWidget(_hline())
        body.addWidget(clear_flagged)
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


# ── menu entries ──────────────────────────────────────────────────────────────

_DM_NAMES = {
    AddWordDialog:          AddWordDialog._DM_NAME,
    BackfillDialog:         BackfillDialog._DM_NAME,
    FindDuplicatesDialog:   FindDuplicatesDialog._DM_NAME,
    BatchOperationsDialog:  BatchOperationsDialog._DM_NAME,
}

for _cls, _name in _DM_NAMES.items():
    # 交給 Anki 內建的 dialog manager 管，而不是自己造一個 registry：一次拿到單例、
    # 還原被縮小的視窗、raise、reopen() 重掃，**以及** Anki 退出 / 切 profile 時
    # closeAll() 會來收（自製 registry 它看不到 → worker 會對正在卸載的 collection 續寫）。
    aqt.dialogs.register_dialog(_name, lambda cls=_cls: cls(mw))


def _show_nonmodal(dialog_cls):
    """批次類視窗用非阻塞方式開啟（show() 而非 exec()）：不鎖 Anki 主視窗，
    生成跑很久時可以移開/縮小視窗、繼續用 Anki。
    Settings 不是批次視窗，維持 modal exec()。"""
    dlg = aqt.dialogs.open(_DM_NAMES[dialog_cls])
    dlg.show()
    return dlg


def open_dialog():
    _show_nonmodal(AddWordDialog)

def open_backfill_dialog():
    _show_nonmodal(BackfillDialog)

def open_duplicates_dialog():
    _show_nonmodal(FindDuplicatesDialog)

def open_batch_operations_dialog():
    _show_nonmodal(BatchOperationsDialog)

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
