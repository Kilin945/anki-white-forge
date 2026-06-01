# 例句整句中文翻譯（Sentence_CN）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在卡片背面英文例句下方新增一塊「整句繁體中文翻譯」區域（稍深米色框、點一下顯示），新單字自動產生、舊卡批次回填。

**Architecture:** 新欄位 `Sentence_CN`。翻譯用獨立函式（core `llm_translate_sentence` / addon `_groq_translate_sentence`）。批次回填靠可重用的 `core/rate_limiter.py` 控制每批上限與 429 煞車；resume 靠「只挑缺欄位的卡」天然達成。單張新增不套速率控制。

**Tech Stack:** Python（uv）、pytest、Groq SDK（`llama-3.3-70b-versatile`）、AnkiConnect、addon urllib 直呼 Groq、HTML/CSS 模板。

**Commit 規則：** 每個 py 檔案獨立成自己的 commit，不與模板／文件混。模板（UI）改動等使用者重啟 Anki 驗證滿意後才提交。

> **設計修正（實作中與使用者多輪確認，取代下方 Task 6 §4 / Task 3 / Task 4 的部分內容）：**
> 1. **Sentence_CN 改由專用工具負責**：新增 addon 選單「批次回填整句翻譯」(`SentenceCNDialog` + `SentenceCNWorker`) 與 CLI `backfill_sentence_cn.py`。**⌘S Complete 與 `backfill_words.py` 都不碰 `Sentence_CN`**（completeness 不含它），避免「補全部欄位」觸發未節流大量翻譯。⌘D 仍即時產生。
> 2. **速率＝持續節流（pacing）+ 時間盒選單**：以不超過速率的節奏持續翻譯，撞 429 就等 `Retry-After`（~幾秒）再續，**直到選定秒數用完**（`直接完成`=全部翻完）。選單 `30秒/2分/5分/10分/直接完成`，按鈕顯示預估筆數，開跑前顯示總預估 `⌈N/RPM⌉ 分`，跑時顯示剩餘秒數。（不用 burst：對 <60s 的預算會失效、剛跑過時秒撞 429 只跑 2 秒。）CLI 為 run-to-completion + Ctrl-C 結束。
> 3. 進度 box 分流：`FIELD_BOXES`（⌘D，含 Sentence-CN）vs `BACKFILL_BOXES`（⌘S，不含）。
>
> 詳見 spec §3、§4。下方原始 Task 文字保留作歷程，實作以本註記與 spec 為準。

---

## File Structure

- **Create** `core/rate_limiter.py` — 通用批次節流／429 煞車器，無 Anki 知識，可重用。
- **Modify** `core/llm.py` — 新增 `llm_translate_sentence()` 與 `groq_generate_strict()`。
- **Create** `backfill_sentence_cn.py` — CLI 批次回填腳本，組合 core + rate_limiter。
- **Modify** `backfill_words.py` — completeness 與 `process_note` 接 `Sentence_CN`，抽出 `note_complete()` helper。
- **Modify** `add_word.py` — ⌘D 寫入 `Sentence_CN`。
- **Modify** `addon/__init__.py` — `_groq_translate_sentence`、`_groq_chat_strict`、⌘D 寫欄位、⌘S completeness + 速率煞車 + 進度框、`FIELD_BOXES`。
- **Modify** `templates/back.html` + `templates/style.css` — UI 區塊。
- **Modify** `test_backfill.py` — 新增 core 與批次邏輯的測試。
- **Modify** `README.md` / `CLAUDE.md` — 欄位與用法同步。

---

## Task 1: core 整句翻譯函式 `llm_translate_sentence`

**Files:**
- Modify: `core/llm.py`
- Test: `test_backfill.py`

- [ ] **Step 1: Write the failing test**

在 `test_backfill.py` 末尾新增：

```python
class TestLlmTranslateSentence:
    @patch.object(llm_mod, 'llm')
    def test_returns_chinese(self, mock_llm):
        mock_llm.return_value = "這個系統能妥善處理併發。"
        assert llm_mod.llm_translate_sentence("The system handles concurrency well.") == "這個系統能妥善處理併發。"

    @patch.object(llm_mod, 'llm')
    def test_strips_wrapping_quotes(self, mock_llm):
        mock_llm.return_value = '"這是一隻貓。"'
        assert llm_mod.llm_translate_sentence("This is a cat.") == "這是一隻貓。"

    @patch.object(llm_mod, 'llm')
    def test_rejects_english_preamble(self, mock_llm):
        mock_llm.return_value = "Here is the translation: 這是一隻貓。"
        assert llm_mod.llm_translate_sentence("This is a cat.") == ""

    @patch.object(llm_mod, 'llm')
    def test_rejects_no_chinese(self, mock_llm):
        mock_llm.return_value = "I cannot translate this."
        assert llm_mod.llm_translate_sentence("foo") == ""

    def test_empty_sentence_returns_empty(self):
        assert llm_mod.llm_translate_sentence("") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test_backfill.py::TestLlmTranslateSentence -v`
Expected: FAIL（`AttributeError: module 'core.llm' has no attribute 'llm_translate_sentence'`）

- [ ] **Step 3: Write minimal implementation**

在 `core/llm.py` 頂部 import 區加入 `re`（若尚未 import）與 rate_limiter 例外（Task 2 會建立；此步先不 import strict 路徑）：

```python
import re
```

把第 3 行 `from groq import Groq` 改為：

```python
from groq import Groq, RateLimitError
```

在 `llm_translate` 函式之後新增：

```python
SENTENCE_CN_PROMPT = (
    "Translate this English sentence into natural, complete Traditional Chinese. "
    "Output only the translation. No explanation, no quotes, no English.\n\n"
    'Sentence: "{sentence}"'
)


def _looks_like_chinese_translation(text):
    if not text:
        return False
    if not re.search(r"[一-鿿]", text):   # must contain Chinese
        return False
    if re.search(r"[A-Za-z]{6,}", text):           # long latin run = preamble/refusal
        return False
    return True


def llm_translate_sentence(sentence, *, strict=False):
    """Traditional-Chinese translation of a full English sentence. '' on failure.

    strict=True surfaces Groq 429 as RateLimitReached (for batch jobs);
    otherwise uses the normal swallowing llm() path (single-add / per-card).
    """
    if not sentence:
        return ""
    prompt = SENTENCE_CN_PROMPT.format(sentence=sentence)
    result = groq_generate_strict(prompt) if strict else llm(prompt)
    result = result.strip().strip('"').strip()
    return result if _looks_like_chinese_translation(result) else ""
```

（`groq_generate_strict` 於 Task 2 step 3 一併加入 `core/llm.py`；本步先讓非 strict 路徑與測試通過。）

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test_backfill.py::TestLlmTranslateSentence -v`
Expected: PASS（5 passed）

- [ ] **Step 5: Commit**

```bash
git add core/llm.py test_backfill.py
git commit -m "feat: core 新增 llm_translate_sentence 整句中文翻譯"
```

---

## Task 2: 可重用速率控制 `core/rate_limiter.py`

**Files:**
- Create: `core/rate_limiter.py`
- Modify: `core/llm.py`（新增 `groq_generate_strict`）
- Test: `test_backfill.py`

- [ ] **Step 1: Write the failing test**

在 `test_backfill.py` 頂部 import 區加入：

```python
from core.rate_limiter import BatchLimiter, RateLimitReached, is_rate_limit_error
```

末尾新增：

```python
class TestBatchLimiter:
    def test_new_limiter_continues(self):
        lim = BatchLimiter(batch_limit=3)
        assert lim.should_continue() is True

    def test_stops_at_batch_limit(self):
        lim = BatchLimiter(batch_limit=2)
        lim.record_success(); lim.record_success()
        assert lim.should_continue() is False
        assert lim.stopped_reason == "batch_limit"

    def test_stops_on_rate_limited(self):
        lim = BatchLimiter(batch_limit=99)
        lim.record_rate_limited()
        assert lim.should_continue() is False
        assert lim.stopped_reason == "rate_limited"


class TestIsRateLimitError:
    def test_rate_limit_reached(self):
        assert is_rate_limit_error(RateLimitReached()) is True

    def test_code_429(self):
        e = Exception(); e.code = 429
        assert is_rate_limit_error(e) is True

    def test_status_code_429(self):
        e = Exception(); e.status_code = 429
        assert is_rate_limit_error(e) is True

    def test_message_contains_429(self):
        assert is_rate_limit_error(Exception("Error 429 rate limit")) is True

    def test_other_error_false(self):
        assert is_rate_limit_error(Exception("boom")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test_backfill.py::TestBatchLimiter test_backfill.py::TestIsRateLimitError -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'core.rate_limiter'`）

- [ ] **Step 3: Write minimal implementation**

建立 `core/rate_limiter.py`：

```python
"""Reusable batch throttle / 429 brake for rate-limited API loops (e.g. Groq).

Pure batch accounting — no Anki or field knowledge. Import from any batch job.
"""


class RateLimitReached(Exception):
    """Raised by API helpers when the provider returns HTTP 429."""


def is_rate_limit_error(exc):
    """True if exc represents HTTP 429 (urllib HTTPError, groq RateLimitError, etc.)."""
    if isinstance(exc, RateLimitReached):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    return "429" in str(exc)


class BatchLimiter:
    """Counts successful calls; stops a batch at batch_limit or on rate-limit."""

    def __init__(self, batch_limit=25):
        self.batch_limit = batch_limit
        self.processed = 0
        self.stopped_reason = None   # None | "batch_limit" | "rate_limited"

    def should_continue(self):
        if self.stopped_reason:
            return False
        if self.processed >= self.batch_limit:
            self.stopped_reason = "batch_limit"
            return False
        return True

    def record_success(self):
        self.processed += 1

    def record_rate_limited(self):
        self.stopped_reason = "rate_limited"
```

在 `core/llm.py` 的 `groq_generate` 之後新增（surface 429）：

```python
from core.rate_limiter import RateLimitReached


def groq_generate_strict(prompt):
    """Like groq_generate but raises RateLimitReached on 429 — for batch jobs."""
    if not _groq_client:
        return ""
    try:
        resp = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=200,
        )
        return resp.choices[0].message.content.strip()
    except RateLimitError:
        raise RateLimitReached()
    except Exception as e:
        print(f"  [groq error] {e}")
        return ""
```

> 注意：`core.llm` import `core.rate_limiter`，反向不可，避免循環。

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test_backfill.py -v`
Expected: PASS（含 Task 1 的測試，全綠）

- [ ] **Step 5: Commit**

```bash
git add core/rate_limiter.py core/llm.py test_backfill.py
git commit -m "feat: 新增可重用 rate_limiter 與 groq_generate_strict（429 煞車）"
```

---

## Task 3: CLI 批次回填腳本 `backfill_sentence_cn.py`

**Files:**
- Create: `backfill_sentence_cn.py`
- Test: `test_backfill.py`

- [ ] **Step 1: Write the failing test**

在 `test_backfill.py` 頂部 import 區加入：

```python
import backfill_sentence_cn as bf_cn
```

末尾新增：

```python
def _note(nid, sentence, cn=""):
    return {"noteId": nid, "fields": {
        "Sentence": {"value": sentence}, "Sentence_CN": {"value": cn}}}


class TestPendingNotes:
    def test_picks_missing_cn_with_sentence(self):
        notes = [_note(1, "A cat.", ""), _note(2, "A dog.", "一隻狗。")]
        assert [n["noteId"] for n in bf_cn.pending_notes(notes)] == [1]

    def test_skips_when_no_sentence(self):
        notes = [_note(1, "", "")]
        assert bf_cn.pending_notes(notes) == []


class TestRunBatch:
    def test_stops_at_batch_limit(self):
        notes = [_note(i, f"Sentence {i}.") for i in range(5)]
        updates = []
        lim = BatchLimiter(batch_limit=2)
        done, remaining = bf_cn.run_batch(
            notes, translate=lambda s: "譯文", update=lambda nid, cn: updates.append(nid), limiter=lim)
        assert done == 2
        assert remaining == 3
        assert lim.stopped_reason == "batch_limit"
        assert updates == [0, 1]

    def test_stops_on_rate_limit(self):
        notes = [_note(i, f"Sentence {i}.") for i in range(5)]
        calls = {"n": 0}
        def translate(s):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RateLimitReached()
            return "譯文"
        lim = BatchLimiter(batch_limit=99)
        done, remaining = bf_cn.run_batch(
            notes, translate=translate, update=lambda nid, cn: None, limiter=lim)
        assert done == 1
        assert lim.stopped_reason == "rate_limited"

    def test_skips_empty_translation(self):
        notes = [_note(1, "A cat."), _note(2, "A dog.")]
        updates = []
        lim = BatchLimiter(batch_limit=99)
        done, remaining = bf_cn.run_batch(
            notes, translate=lambda s: "" if s == "A cat." else "一隻狗。",
            update=lambda nid, cn: updates.append(nid), limiter=lim)
        assert done == 1
        assert updates == [2]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test_backfill.py::TestPendingNotes test_backfill.py::TestRunBatch -v`
Expected: FAIL（`ModuleNotFoundError: No module named 'backfill_sentence_cn'`）

- [ ] **Step 3: Write minimal implementation**

建立 `backfill_sentence_cn.py`：

```python
#!/usr/bin/env python3
"""Batch backfill of Sentence_CN (full-sentence Chinese translation) for existing cards.

Resume is automatic: each run only picks cards still missing Sentence_CN, so it is
safe to Ctrl-C any time and re-run to continue. Stops after one batch (rate-limit
protection) and tells you to re-run after ~60s.
"""
from core.anki import anki
from core.llm import llm_translate_sentence
from core.text import strip_html
from core.rate_limiter import BatchLimiter, RateLimitReached

DECK_NAME = "My_Daily_English"


def pending_notes(notes):
    """Notes that HAVE a sentence but are missing Sentence_CN."""
    out = []
    for n in notes:
        sentence = strip_html(n["fields"]["Sentence"]["value"]).strip()
        cn = n["fields"].get("Sentence_CN", {}).get("value", "").strip()
        if sentence and not cn:
            out.append(n)
    return out


def run_batch(notes, translate, update, limiter):
    """Translate pending notes until limiter stops. Returns (done, remaining)."""
    pend = pending_notes(notes)
    done = 0
    for n in pend:
        if not limiter.should_continue():
            break
        sentence = strip_html(n["fields"]["Sentence"]["value"]).strip()
        try:
            cn = translate(sentence)
        except RateLimitReached:
            limiter.record_rate_limited()
            break
        if cn:
            update(n["noteId"], cn)
            limiter.record_success()
            done += 1
        # empty translation → leave Sentence_CN empty, re-picked next run
    remaining = len(pend) - done
    return done, remaining


def _print_box(done, remaining, reason, batch_limit):
    bar = "●" * done
    print("┌─ Sentence_CN 回填 ──────────────────┐")
    print(f"│  {bar}  {done}/{batch_limit}")
    if reason == "rate_limited":
        print("│  ⚠ 偵測到速率上限（429），本批提前停止")
    elif reason == "batch_limit":
        print("│  ⚠ 已達本批上限（速率保護），請 60 秒後再跑")
    if remaining > 0:
        print(f"│  剩餘 {remaining} 張未處理（下次自動從這裡續）")
    else:
        print("│  ✓ 全部完成")
    print("└─────────────────────────────────────┘")


def main():
    print("Fetching notes…")
    ids = anki("findNotes", query=f"deck:{DECK_NAME}")
    notes = anki("notesInfo", notes=ids)
    limiter = BatchLimiter(batch_limit=25)

    def update(note_id, cn):
        anki("updateNoteFields", note={"id": note_id, "fields": {"Sentence_CN": cn}})

    done, remaining = run_batch(
        notes,
        translate=lambda s: llm_translate_sentence(s, strict=True),
        update=update,
        limiter=limiter,
    )
    _print_box(done, remaining, limiter.stopped_reason, limiter.batch_limit)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test_backfill.py -v`
Expected: PASS（全綠）

- [ ] **Step 5: Commit**

```bash
git add backfill_sentence_cn.py test_backfill.py
git commit -m "feat: 新增 backfill_sentence_cn.py 批次回填整句翻譯（含速率煞車與 resume）"
```

---

## Task 4: `backfill_words.py` 接 Sentence_CN

**Files:**
- Modify: `backfill_words.py`
- Test: `test_backfill.py`

- [ ] **Step 1: Write the failing test**

在 `test_backfill.py` 頂部 import 區加入：

```python
import backfill_words as bw
```

末尾新增：

```python
def _full_note(sentence="A cat.", img='<img src="x.jpg">', audio="[sound:a.mp3]",
               front_audio="[sound:b.mp3]", translation="貓", sentence_cn="一隻貓。"):
    return {"fields": {
        "Sentence": {"value": sentence},
        "Image_Prompt": {"value": img},
        "Audio": {"value": audio},
        "Front_Audio": {"value": front_audio},
        "Translation": {"value": translation},
        "Sentence_CN": {"value": sentence_cn},
    }}


class TestNoteComplete:
    def test_full_note_is_complete(self):
        assert bw.note_complete(_full_note()) is True

    def test_missing_sentence_cn_incomplete(self):
        assert bw.note_complete(_full_note(sentence_cn="")) is False

    def test_missing_translation_incomplete(self):
        assert bw.note_complete(_full_note(translation="")) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest test_backfill.py::TestNoteComplete -v`
Expected: FAIL（`AttributeError: module 'backfill_words' has no attribute 'note_complete'`）

- [ ] **Step 3: Write minimal implementation**

在 `backfill_words.py` import 區補上（若缺）`from core.llm import llm_translate_sentence`，並新增可重用 helper（放在 `process_note` 之前）：

```python
def note_complete(n):
    """True if a note has every auto-filled field, including Sentence_CN."""
    f = n["fields"]
    sentence = strip_html(f["Sentence"]["value"])
    return (
        bool(sentence) and not is_placeholder(sentence) and
        has_image(f["Image_Prompt"]["value"]) and
        bool(f["Audio"]["value"]) and
        bool(f.get("Front_Audio", {}).get("value", "")) and
        bool(f.get("Translation", {}).get("value", "")) and
        bool(f.get("Sentence_CN", {}).get("value", ""))
    )
```

在 `process_note` 內，於讀取既有欄位處加入：

```python
    current_sentence_cn = note["fields"].get("Sentence_CN", {}).get("value", "")
```

把原本的

```python
    has_translation = bool(current_translation)

    if has_sentence and has_img and has_audio and has_front_audio and has_translation:
        return word, "skipped"
```

改為：

```python
    has_translation = bool(current_translation)
    has_sentence_cn = bool(current_sentence_cn)

    if note_complete(note):
        return word, "skipped"
```

在 `ThreadPoolExecutor` 區塊內，`translation` future 之後新增整句翻譯（句子重生時也要重翻）：

```python
        if not has_sentence_cn or need_sentence:
            futures["sentence_cn"] = pool.submit(llm_translate_sentence, sentence)
```

並於收集結果處（`translation` 區塊之後）新增：

```python
        if "sentence_cn" in futures:
            cn = futures["sentence_cn"].result()
            if cn:
                fields["Sentence_CN"] = cn
                lines.append(f"  整句譯   : {cn}")
```

把 `main()` 內的 `pending` 過濾改用 helper：

```python
    pending = [n for n in notes if not note_complete(n)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest test_backfill.py -v`
Expected: PASS（全綠）

- [ ] **Step 5: Commit**

```bash
git add backfill_words.py test_backfill.py
git commit -m "feat: backfill_words 接 Sentence_CN（completeness + 句子重生連動重翻）"
```

---

## Task 5: `add_word.py` ⌘D 寫入 Sentence_CN

**Files:**
- Modify: `add_word.py`

- [ ] **Step 1: 確認既有 import 與流程**

Run: `grep -n "from core.llm\|translation_result\|trans_thread\|addNote" add_word.py`
Expected: 看到 `from core.llm import ...`、`do_translate`/`trans_thread`、`addNote` 欄位區塊（約 145 行）。

- [ ] **Step 2: 在 import 補上翻譯函式**

把 `add_word.py` 的 `from core.llm import ...` 那行補上 `llm_translate_sentence`（若尚未含）。例如：

```python
from core.llm import llm_sentence_and_query, llm_translate, llm_translate_sentence
```

（以實際既有 import 清單為準，只是把 `llm_translate_sentence` 加進去。）

- [ ] **Step 3: 句子確定後翻譯整句**

在 `img_thread.join()` / `trans_thread.join()` 之後、組 `note` 之前，新增：

```python
    sentence_cn = llm_translate_sentence(sentence)
    print(f"  整句譯: {sentence_cn or '⚠️'}")
```

在 `addNote` 的 `fields` 字典內，`"Translation": translation,` 之後加一行：

```python
                "Sentence_CN": sentence_cn,
```

- [ ] **Step 4: 語法檢查**

Run: `uv run python -c "import ast; ast.parse(open('add_word.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add add_word.py
git commit -m "feat: add_word ⌘D 自動產生並寫入 Sentence_CN"
```

---

## Task 6: addon 接線（`addon/__init__.py`）

**Files:**
- Modify: `addon/__init__.py`

> addon 跑在 Anki 的 Python，不能 import `core/`；翻譯用 urllib 直呼 Groq 的 `_groq_chat`。本任務無自動測試，靠**重啟 Anki 手動驗證**（見 Step 6）。

- [ ] **Step 1: 新增 strict 版 `_groq_chat`（surface 429）**

在 `_groq_chat`（約 81 行）之後新增模組層函式：

```python
class _AddonRateLimited(Exception):
    pass


def _groq_chat_strict(prompt, *, temperature, max_tokens, timeout):
    """Like _groq_chat but raises _AddonRateLimited on HTTP 429 — for batch (⌘S)."""
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
            return json.loads(r.read().decode())["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise _AddonRateLimited()
        return ""
    except Exception:
        return ""
```

- [ ] **Step 2: 新增 `_groq_translate_sentence` 方法**

在 `Worker._groq_translate`（約 229 行）之後新增：

```python
    def _groq_translate_sentence(self, sentence, *, strict=False):
        """Traditional Chinese translation of a full sentence. '' on failure."""
        if not sentence:
            return ""
        prompt = ('Translate this English sentence into natural, complete Traditional '
                  'Chinese. Output only the translation. No explanation, no quotes, no '
                  f'English.\n\nSentence: "{sentence}"')
        chat = _groq_chat_strict if strict else _groq_chat
        reply = chat(prompt, temperature=0.3, max_tokens=200, timeout=15).strip().strip('"').strip()
        if not re.search(r"[一-鿿]", reply):     # no Chinese → fail
            return ""
        if re.search(r"[A-Za-z]{6,}", reply):            # long English run → preamble
            return ""
        return reply
```

- [ ] **Step 3: ⌘D 建立卡片時寫入 Sentence_CN**

在 ⌘D 流程組 `note` 欄位處（約 480-486 行，`note["Sentence"] = data["sentence"]` 附近）加入：

```python
            note["Sentence_CN"] = self._groq_translate_sentence(data["sentence"])
```

（與 `Translation` 同一段、用同一個 `data["sentence"]`。）

- [ ] **Step 4: ⌘S 完整性判斷 + 欄位寫入 + 速率煞車**

4a. `FIELD_BOXES`（約 42 行）加入整句翻譯項：

```python
FIELD_BOXES = [("sentence", "Sentence"), ("sentence_cn", "Sentence_CN"),
               ("image", "Image"), ...]   # 既有其餘項保留
```

4b. ⌘S 掃描完整性（約 711-730 行）加入 `Sentence_CN`：

```python
            has_sentence_cn = bool(note["fields"].get("Sentence_CN", {}).get("value", "")) \
                if "fields" in note else bool(note.get("Sentence_CN", ""))
```

並把 `incomplete = (...)` 條件補上 `or not has_sentence_cn`，逐卡欄位狀態 dict 補 `"sentence_cn": has_sentence_cn`。

4c. ⌘S 生成欄位處（約 592、627 行 translation 附近）加入整句翻譯，並以 `_AddonRateLimited` 煞車整批：

```python
                try:
                    note["fields"]["Sentence_CN"] = {
                        "value": self._w._groq_translate_sentence(s, strict=True)}
                except _AddonRateLimited:
                    self._w._rate_limited = True   # 旗標：停止後續卡片、UI 顯示「遞 60 秒」
```

在 BackfillWorker 主迴圈每張卡開始前檢查 `self._rate_limited`，為真則停止並透過既有進度 signal 顯示「已達速率上限，請 60 秒後再 ⌘S」。

> 確切行號以實際檔案為準；沿用既有 BackfillWorker 進度框 UI（每卡一列），達上限時於框內顯示提示。

- [ ] **Step 5: 語法檢查**

Run: `uv run python -c "import ast; ast.parse(open('addon/__init__.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 6: 手動驗證（重啟 Anki）**

1. 重啟 Anki（addon 為 symlink，改完需重啟）。
2. ⌘D 新增一個測試單字 → 開卡片背面，確認 `Sentence_CN` 有整句中文。
3. ⌘S Complete → 確認舊卡開始補 `Sentence_CN`，進度框每卡一列正常。
4. 連續 ⌘S 直到接近速率上限 → 確認出現「遞 60 秒」提示而非當掉。

- [ ] **Step 7: Commit**

```bash
git add addon/__init__.py
git commit -m "feat: addon ⌘D/⌘S 接 Sentence_CN（含 429 煞車與進度框）"
```

---

## Task 7: 模板 UI（`back.html` + `style.css`）

**Files:**
- Modify: `templates/back.html`
- Modify: `templates/style.css`

> UI 改動，**等使用者重啟 Anki 驗證滿意後才 commit**（依專案規則）。

- [ ] **Step 1: back.html 插入翻譯框**

在 `templates/back.html` 的 `.sentence` `<div>`（第 12 行）與 `.play-row`（第 13 行）之間插入：

```html
    <div class="sentence-cn" onclick="this.classList.toggle('revealed')">{{Sentence_CN}}</div>
```

- [ ] **Step 2: style.css 新增樣式**

在 `templates/style.css` 的 `.sentence .hl` 規則（約 167 行）之後新增：

```css
.sentence-cn {
  margin-top: 4px;
  padding: 14px 18px;
  background: #F0EBE3;          /* 稍深米色，與 #FDFBF7 大背景區別 */
  border: 1px solid #E2DDD5;
  border-radius: 10px;
  font-size: clamp(20px, 3vw, 26px);
  line-height: 1.7;
  color: transparent;          /* 預設藏字，只見深色空框 */
  cursor: pointer;
  user-select: none;
  transition: color 0.2s;
}
.sentence-cn.revealed { color: #475569; }
```

在手機 RWD 區塊（`@media (max-width: 600px)`，約 264 行 `.sentence` 之後）新增：

```css
  .sentence-cn {
    padding: 10px 14px;
    font-size: clamp(16px, 4vw, 19px);
    text-align: left;
  }
```

- [ ] **Step 3: 部署模板**

Run: `uv run python update_template.py`
Expected: `Updating template… / Updating CSS… / Done.`

- [ ] **Step 4: 手動驗證**

桌機與手機（同步後）各確認：翻譯框顯示為稍深米色、點一下顯示／再點隱藏、空翻譯時為空框不突兀、RWD 縮放正常、不擋 play button。

- [ ] **Step 5: Commit（使用者驗證滿意後）**

```bash
git add templates/back.html templates/style.css
git commit -m "feat: 卡片背面新增整句中文翻譯區塊（稍深米色框、點擊顯示）"
```

---

## Task 8: 新增欄位、校準與文件

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: 在筆記類型新增 `Sentence_CN` 欄位**

確保 AnkiConnect 開著，執行：

```bash
uv run python -c "from core.anki import anki; anki('modelFieldAdd', modelName='English_White_Method', fieldName='Sentence_CN', index=99); print('ok')"
```

Expected: `ok`（`index=99` 會落在最後；若要精準排在 `Sentence` 後，於 Anki GUI 拖動，或改 index 為 `Sentence` 之後的位置）。
驗證：`uv run python -c "from core.anki import anki; print(anki('modelFieldNames', modelName='English_White_Method'))"` 應含 `Sentence_CN`。

- [ ] **Step 2: dry-run 校準 batch_limit**

連續呼叫翻譯 API（**不寫卡片**）直到第一次 429，記錄成功數：

```bash
uv run python -c "
from core.llm import llm_translate_sentence
from core.rate_limiter import RateLimitReached
import time
n=0; t=time.time()
try:
    while True:
        if not llm_translate_sentence('This is a calibration sentence number %d.'%n, strict=True):
            pass
        n+=1
        print(f'{n} ok ({n/((time.time()-t)/60):.0f}/min)', end='\r')
except RateLimitReached:
    print(f'\n429 after {n} calls in {time.time()-t:.0f}s')
"
```

依結果把 `backfill_sentence_cn.py` 與 addon 的 `batch_limit`（預設 25）調成實測安全值（建議取實測值的 ~80%）。若調整了，分別 commit 該 py。

- [ ] **Step 3: 跑批次回填既有舊卡**

```bash
uv run python backfill_sentence_cn.py
```

達上限會自動停並提示，等 ~60 秒重跑，直到顯示「全部完成」。

- [ ] **Step 4: 更新文件**

- `README.md`：牌組欄位表新增 `Sentence_CN`（整句中文翻譯）、卡片背面說明與點擊顯示行為、`backfill_sentence_cn.py` 用法（使用面事實的單一來源）。
- `CLAUDE.md`：Key Rules 補一條 ——「`Sentence_CN`（整句翻譯）由 `llm_translate_sentence` / `_groq_translate_sentence` 共用，⌘D 即時生成、⌘S 與 `backfill_sentence_cn.py` 批次補；批次走 `core/rate_limiter.py` 煞車，單張新增不套」。確認同一事實不與 README 重複。

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: 同步 Sentence_CN 欄位、用法與架構約束"
```

---

## Self-Review

**Spec coverage：**
- 新欄位 `Sentence_CN` → Task 8 Step 1。
- 獨立翻譯函式（core / addon）→ Task 1 / Task 6 Step 2。
- 三處接線（⌘D / ⌘S / backfill）→ Task 5 / Task 6 / Task 4 + Task 3。
- 完整性判斷更新 → Task 4（`note_complete`）、Task 6 Step 4。
- 句子變動重翻 → Task 4 Step 3（`or need_sentence`）。
- 速率控制可重用模組 → Task 2。
- 批次行為 + resume → Task 3（`pending_notes` 天然 resume、`run_batch` 煞車）。
- batch_limit 校準 → Task 8 Step 2。
- CLI 進度顯示區別 → Task 3（`_print_box`）。
- UI 稍深米色框 + 點擊顯示 + RWD → Task 7。
- 部署步驟 → Task 7 Step 3、Task 8。

**Placeholder scan：** 無 TBD/TODO；addon 行號標「以實際檔案為準」因 symlink 檔案會隨開發微移，已附定位 `grep`/錨點。

**Type consistency：** `llm_translate_sentence(sentence, *, strict=False)`、`BatchLimiter(batch_limit=)` / `should_continue` / `record_success` / `record_rate_limited` / `stopped_reason`、`RateLimitReached`、`is_rate_limit_error`、`note_complete(n)`、`pending_notes(notes)`、`run_batch(notes, translate, update, limiter)` 在各 Task 間命名一致。

**開放項目：** `batch_limit` 實測值（Task 8 Step 2 後定）；addon ⌘S 速率提示文案最終定稿。
