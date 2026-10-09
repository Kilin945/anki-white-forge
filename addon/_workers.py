"""三個 QThread worker：⌘A `Worker`、⌘S `BackfillWorker`、⌘F 批次翻譯 `SentenceCNWorker`。"""

import os
import re
import json
import time
import threading
import subprocess
import urllib.request
import urllib.error
from aqt.qt import (
    QThread, pyqtSignal,
)
from . import _llm_dispatch as _lld
from . import _llm
from . import _examples
from ._config import ANKI_URL, GTTS_SCRIPT, IMAGE_SCRIPT, MAX_BACKFILL_WORKERS, PLACEHOLDERS, SHORT_WALL_WAIT, VENV_PYTHON, VOICE_SENTENCE, VOICE_WORD
from ._text import _accept_word_translation, _clean_text, _key_error_lines, _looks_like_chinese_translation, _need_sentence_audio, _sentence_acceptable, _sentence_has_word, _sentence_reason_text, _sentence_to_write, _sentence_usable, _translation_reject_reason, record_rejected_translation
from ._llm import _AddonRateLimited, _sentence_prompt, _to_traditional_logged
from ._images import _image_alt, _image_html

_log = _lld.get_logger()   # 批次/LLM 事件集中記錄到 logs/addon_llm.log（gitignored）


class Worker(QThread):
    step     = pyqtSignal(str, str, str)   # (field key, state: "ok" / "warn", 退回原因短句，ok 時為 "")
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

            # 圖先行：依「單字＋詞義」搜圖，再依照片描述造句（句子配圖）。圖不再依賴句子，
            # 句子生成失敗圖照留。
            reasons = {}                      # field key → 畫面短句（完整原因在 log）
            # 詞義只選一次：沒提示就先選，搜圖與造句共用（不寫回 Association 欄位）
            hint = self.association or self._pick_sense(word)
            image_field = self._fetch_image(word, definition=hint)
            self.step.emit("image", "ok" if image_field else "warn",
                           "" if image_field else "no image")
            photo = _image_alt(image_field)

            sentence, engine = self._llm_sentence(word, hint, photo=photo)
            if not sentence:
                sentence = f"Please add an example sentence for '{word}'."
            sentence_ok = _sentence_usable(sentence)
            self.step.emit("sentence", "ok" if sentence_ok else "warn",
                           "" if sentence_ok else _sentence_reason_text(engine))

            # Translation and Audio in parallel — but only when the sentence is
            # usable: 依賴句意的下游（翻譯/句音）拿佔位符當輸入會做出彼此不一致
            # 的髒卡（例：Sentence_CN 是佔位符的翻譯）→ 全部跳過亮橘，之後 ⌘S 連同
            # 句子一起重做。Front_Audio（單字音）與句子無關，照做。圖已在上面先抓。
            translation_result = [""]
            sentence_cn_result = [""]
            audio_filename = f"{word}_tts.mp3" if sentence_ok else ""
            front_audio_filename = f"{word}_word.mp3"

            def do_translate():
                translation_result[0] = self._groq_translate(word, sentence, reasons=reasons)
                sentence_cn_result[0] = self._groq_translate_sentence(sentence, word=word, reasons=reasons)

            trans_thread = None
            if sentence_ok:
                trans_thread = threading.Thread(target=do_translate)
                trans_thread.start()

            audio_items = [
                {"text": word, "filepath": os.path.join(self.media_dir, front_audio_filename), "voice": VOICE_WORD},
            ]
            if sentence_ok:
                audio_items.append(
                    {"text": sentence, "filepath": os.path.join(self.media_dir, audio_filename), "voice": VOICE_SENTENCE})
            try:
                self._make_audio_batch(audio_items)
                self.step.emit("audio", "ok" if sentence_ok else "warn",
                               "" if sentence_ok else "skipped: no sentence")
            finally:
                if trans_thread:
                    trans_thread.join()    # always join so the thread doesn't leak on audio failure

            if not sentence_ok:
                reasons["translation"] = reasons["sentence_cn"] = "skipped: no sentence"
            self.step.emit("translation", "ok" if translation_result[0] else "warn",
                           "" if translation_result[0] else reasons.get("translation", ""))
            self.step.emit("sentence_cn", "ok" if sentence_cn_result[0] else "warn",
                           "" if sentence_cn_result[0] else reasons.get("sentence_cn", ""))

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

    def _pick_sense(self, word):
        """沒有 Association 時選一次詞義（⌘A／⌘S 共用；測試替換這個方法）。"""
        return _llm._llm_pick_sense(word)

    def _groq_sentence(self, word, association="", photo="", must_contain=False, examples=()):
        # 造句是多條件約束任務 → 較高思考等級（其餘呼叫維持預設 low）
        return _llm._groq_chat(_sentence_prompt(word, association, photo, must_contain=must_contain,
                                         examples=examples),
                          temperature=0.7, max_tokens=200, timeout=15, effort="medium",
                          task="sentence")

    def _llm_sentence(self, word, association="", photo=""):
        """(句子, 引擎或退回原因)。句子裡沒出現單字（卡片高亮不到）就帶 must_contain 重問一次，
        重問仍沒有也照收、只記 log——硬擋會誤殺 sweep→swept 這種合法句。
        KEEP-IN-SYNC: core/llm.py::llm_sentence（同樣的重問邏輯）。"""
        ex = _examples.examples_for(word, association)   # 每張卡只查一次；失敗回 []
        result = self._groq_sentence(word, association, photo, examples=ex)
        if not _sentence_acceptable(result):
            reason = "no-reply" if not (result or "").strip() else "not-a-clean-sentence"
            _log.warning("sentence rejected word=%s reason=%s reply=%r", word, reason, result)
            return "", reason      # 回空 → 上層退 placeholder，等下次補；第二值給畫面分「沒回」與「回了垃圾」
        if not _sentence_has_word(word, result):
            _log.info("sentence word-missing word=%s reply=%r → retry with must-contain", word, result)
            retry = self._groq_sentence(word, association, photo, must_contain=True, examples=ex)
            if _sentence_acceptable(retry):
                if not _sentence_has_word(word, retry):
                    _log.warning("sentence word-missing after retry word=%s reply=%r (kept)", word, retry)
                return retry, "Groq"
        return result, "Groq"

    def _groq_translate(self, word, sentence, reasons=None):
        """Traditional Chinese meaning of word AS USED IN the sentence ('' on failure).
        Proper nouns (frameworks/products) stay in English.
        reasons：呼叫端給的 dict，被退時寫入 reasons["translation"]＝畫面短句（完整原因在 log）。"""
        prompt = (f'Give the Traditional Chinese meaning of "{word}" as it is used in this '
                  f'sentence: "{sentence}". Give ONE concise translation only — do NOT list '
                  f'synonyms or near-duplicate terms (e.g. never "水杯、茶杯"). If "{word}" is a '
                  f'product / framework / library / tool proper noun (e.g. Spring, React, Docker, '
                  f'Hazelcast), do NOT translate it — output the English name as-is. Keep it short '
                  f'(usually 1-4 characters; a little longer only if a single term genuinely needs '
                  f'it). Write Traditional Chinese as used in Taiwan; never use Simplified Chinese '
                  f'characters. Output only the Chinese, or for a proper noun the English name, '
                  f'no explanation.')
        reply = _llm._groq_chat(prompt, temperature=0.3, max_tokens=32, timeout=10)
        reply = _to_traditional_logged(word, reply)
        accepted = _accept_word_translation(word, reply)
        if not accepted:
            category, short = _translation_reject_reason(word, reply)
            _log.warning("translation rejected word=%s reason=%s reply=%r sentence=%r",
                         word, category, reply, sentence)
            if reasons is not None:
                reasons["translation"] = short
        return accepted

    def _groq_translate_sentence(self, sentence, *, strict=False, word="", reasons=None):
        """Traditional Chinese translation of a full sentence. '' on failure.
        strict=True raises _AddonRateLimited on 429 (for the 批次回填 burst engine).
        reasons：呼叫端給的 dict，被退時寫入 reasons["sentence_cn"]＝畫面短句（完整原因在 log）。"""
        if not sentence:
            return ""
        prompt = ('Translate this English sentence into natural, complete Traditional '
                  'Chinese. Keep product / framework / library / tool proper nouns (e.g. '
                  'Spring, React, Hazelcast) in English inside the translation; do not '
                  'translate such names literally. Write Traditional Chinese as used in Taiwan; '
                  'never use Simplified Chinese characters. '
                  'Output only the translation. No explanation, '
                  f'no quotes.\n\nSentence: "{sentence}"')
        reply = _llm._groq_chat(prompt, temperature=0.3, max_tokens=200, timeout=15,
                           strict=strict).strip().strip('"').strip()
        reply = _to_traditional_logged(word, reply)
        if _looks_like_chinese_translation(reply):
            return reply
        if reply and re.search(r"[一-鿿]", reply):        # 只記「英文字太多」造成的誤殺;沒中文＝沒翻／洩漏,不記
            _log.warning("sentence_cn rejected word=%s reason=too-much-english reply=%r sentence=%r",
                         word, reply, sentence)
            if reasons is not None:
                reasons["sentence_cn"] = "too much English"      # 引文在 log；畫面只放原因
            return self._rejected_translation(word, reply)
        _log.warning("sentence_cn rejected word=%s reason=%s reply=%r sentence=%r",
                     word, "no-reply" if not reply else "no-chinese", reply, sentence)
        if reasons is not None:
            reasons["sentence_cn"] = "no reply" if not reply else "no Chinese"
        return ""

    def _rejected_translation(self, word, reply):
        """記錄被丟掉的翻譯；新待審 term 累積到 self.new_pending_terms（⌘S 收尾提示用）。"""
        added = record_rejected_translation(word, reply)
        sink = getattr(self, "new_pending_terms", None)
        if sink is not None:
            sink.extend(added)
        return ""

    def _fetch_image(self, word, definition=""):
        filename = f"{word}_img_{int(time.time())}.jpg"
        filepath = os.path.join(self.media_dir, filename)
        cmd = [VENV_PYTHON, IMAGE_SCRIPT]
        if definition:
            cmd.extend(["--definition", definition])
        cmd.extend(["--query", _llm._llm_image_query(word, definition)])
        cmd.extend(["--", word, filepath])
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
        except Exception as e:          # timeout / spawn failure → no image; leave blank for ⌘S to retry
            _log.warning("image rejected word=%s reason=helper-failed error=%r", word, e)
            return ""                   # called synchronously; must not raise, a failure just leaves the image blank
        if result.returncode != 0:
            _log.warning("image rejected word=%s reason=no-result stderr=%r", word,
                         (result.stderr or "").strip()[-300:])
        if result.returncode == 0:
            alt = attribution = source = ""
            for line in result.stdout.splitlines():
                if line.startswith("ALT: "):
                    alt = line[len("ALT: "):]
                elif line.startswith("ATTRIBUTION: "):
                    attribution = line[len("ATTRIBUTION: "):]
                elif line.startswith("SOURCE: "):
                    source = line[len("SOURCE: "):].strip()
            return _image_html(filename, alt, attribution, source)
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


class BackfillWorker(QThread):
    step      = pyqtSignal(object, str, str, str)   # (note_id, field, state, 退回原因短句) — object: note ids exceed 32-bit int
    card_done = pyqtSignal(object)             # (note_id) 這張處理完了 —— 不等於補齊了
                                               # (欄位生失敗只會 emit step warn,不中斷)
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
        self.limit_resets = {}         # 撞牆那個池的各模型恢復秒數(對話框訊息用)
        self.limit_pools = []          # 撞牆的池名("sentence"／"light")
        self.new_pending_terms = []    # 本輪翻譯被丟掉、新記進待審的術語(收尾提示用);
        self._w.new_pending_terms = self.new_pending_terms   # 同一個 list,_w 在 worker 執行緒裡 extend

    def stop(self):
        """請 worker 收工：剩下的卡直接跳過（同 _hit_limit 的既有跳過機制）。"""
        self._stopped = True

    def _wall_for(self, need_sentence):
        """這張卡會用到的池裡，最久的牆（0＝都有模型可用）。輕量池一定用到（翻譯）。"""
        tasks = ["light"] + (["sentence"] if need_sentence else [])
        walls = {t: _llm._dispatcher.wall_secs(t) for t in tasks}
        self._blocked_tasks = [t for t, w in walls.items() if w > 0]
        return max(walls.values())

    def _process_one(self, note):
        note_id = note["noteId"]
        word = _clean_text(note["fields"]["Front"]["value"], lower=True)
        # Deterministic rate-limit gate: near the cloud limit → stop here and skip the rest
        # immediately (the dialog says how many are left). No waiting.
        if self._hit_limit or self._stopped:
            return f"skip {word}"
        current = note["fields"]["Sentence"]["value"]
        need_sentence = not current or any(p in current for p in PLACEHOLDERS)
        wall = self._wall_for(need_sentence)
        if 0 < wall <= SHORT_WALL_WAIT:
            _log.info("short wall %.1fs — waiting", wall)
            time.sleep(wall + 0.2)             # 短牆:等掉它,額度窗口一過就續跑
            wall = self._wall_for(need_sentence)
        if wall > 0:
            self._hit_limit = True
            self.retry_after = max(self.retry_after, int(wall) + 1)
            self.limit_pools = list(self._blocked_tasks)
            self.limit_resets = {}         # 只收撞牆的池,別把還有額度的池混進來
            for t in self.limit_pools:
                self.limit_resets.update(_llm._dispatcher.resets(t))
            _log.warning("⌘S stopped: resets=%s", self.limit_resets)
            return f"skip {word}"
        fields = {}
        reasons = {}                      # field key → 畫面短句（完整原因在 log）

        assoc = _clean_text(note["fields"].get("Association", {}).get("value", ""))
        # 詞義只選一次：圖和句子都要重做、又沒提示時，先選詞義給兩邊共用。
        # 只缺其中一樣時不選——另一樣已經定了詞義，另外選反而可能對不上。
        if not assoc and need_sentence and "<img" not in note["fields"]["Image_Prompt"]["value"]:
            assoc = self._w._pick_sense(word)

        # 圖先行：圖不依賴句子（依單字＋詞義搜），先補圖，句子再依照片描述造。
        need_image = "<img" not in note["fields"]["Image_Prompt"]["value"]
        if need_image:
            image_html = self._w._fetch_image(word, definition=assoc)
            fields["Image_Prompt"] = image_html or ""
            self.step.emit(note_id, "image", "ok" if image_html else "warn",
                           "" if image_html else "no image")
        photo = _image_alt(fields.get("Image_Prompt") or note["fields"]["Image_Prompt"]["value"])

        if need_sentence:
            sentence, engine = self._w._llm_sentence(word, assoc, photo=photo)
            to_write = _sentence_to_write(current, sentence, word)
            if to_write is not None:
                fields["Sentence"] = to_write
                sentence = to_write
                ok = not any(p in to_write for p in PLACEHOLDERS)
                self.step.emit(note_id, "sentence", "ok" if ok else "warn",
                               "" if ok else _sentence_reason_text(engine))
            else:
                sentence = _clean_text(current)   # keep the real sentence — don't overwrite with a placeholder
                self.step.emit(note_id, "sentence", "warn", _sentence_reason_text(engine))
                _log.warning("%s: sentence gen failed, kept existing sentence", word)
        else:
            sentence = _clean_text(current)

        need_audio = _need_sentence_audio(note["fields"]["Audio"]["value"],
                                          sentence, "Sentence" in fields)
        need_front = not note["fields"].get("Front_Audio", {}).get("value", "")
        need_translation = not note["fields"].get("Translation", {}).get("value", "")
        need_sentence_cn = not note["fields"].get("Sentence_CN", {}).get("value", "")

        # 句子不可用（生成失敗）→ 依賴句意的下游（翻譯/句中譯）全部跳過亮橘，下次 ⌘S
        # 連同句子一起重做——避免拿佔位符當輸入的髒卡（句音由 _need_sentence_audio 自擋，
        # Front_Audio 與句子無關照做；圖不依賴句子，上面已補）
        if not _sentence_usable(sentence):
            for key, needed in (("translation", need_translation),
                                ("sentence_cn", need_sentence_cn)):
                if needed:
                    self.step.emit(note_id, key, "warn", "skipped: no sentence")
            need_translation = need_sentence_cn = False

        translation_result = [""]
        sentence_cn_result = [""]
        trans_thread = None

        if need_translation or need_sentence_cn:
            def do_translate(w=word, s=sentence):     # both Groq text calls share one thread
                if need_translation:
                    translation_result[0] = self._w._groq_translate(w, s, reasons=reasons)
                if need_sentence_cn:
                    sentence_cn_result[0] = self._w._groq_translate_sentence(s, word=w, reasons=reasons)
            trans_thread = threading.Thread(target=do_translate)
            trans_thread.start()

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
                self.step.emit(note_id, "audio", "ok", "")
        finally:
            if trans_thread:
                trans_thread.join()

        if need_translation:
            if translation_result[0]:
                fields["Translation"] = translation_result[0]
            self.step.emit(note_id, "translation", "ok" if translation_result[0] else "warn",
                           "" if translation_result[0] else reasons.get("translation", ""))
        if need_sentence_cn:
            if sentence_cn_result[0]:
                fields["Sentence_CN"] = sentence_cn_result[0]
            self.step.emit(note_id, "sentence_cn", "ok" if sentence_cn_result[0] else "warn",
                           "" if sentence_cn_result[0] else reasons.get("sentence_cn", ""))

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
        try:   # 把新達標的好卡補進範例索引；失敗只記 log，不擋補卡
            added, removed = _examples.refresh_index(_examples.anki_connect(ANKI_URL))
            if added or removed:
                _log.info("example index: +%d -%d", added, removed)
        except Exception as e:
            _log.warning("example index refresh failed: %r", e)
        start = time.monotonic()
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
        done = sum(1 for r in results if r.startswith("✓"))
        skipped = sum(1 for r in results if r.startswith("skip"))
        _log.info("⌘S run end: done=%d skipped=%d secs=%.1f", done, skipped, time.monotonic() - start)
        self.finished.emit(results)


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
        if self.budget is None:
            return -1
        return max(0, int(self.budget - (time.monotonic() - start)))

    def run(self):
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
                    cn = self._w._groq_translate_sentence(note["sentence"], strict=True,
                                                          word=note.get("word", ""))
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
