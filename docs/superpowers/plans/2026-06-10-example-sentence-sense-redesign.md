# 例句語意品質重構 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓自動生成的例句鎖定正確語意（association → SWE 領域義 → 常用日常義）、短而清楚地表達字義，並讓單字翻譯跟著句子語意且不列重複近義詞。

**Architecture:** 語意決定放在「造句 prompt」內單次完成；單字翻譯依「句中用法」翻譯以保持一致。prompt 邏輯在 `core/llm.py`（CLI 用，可單元測試）與 `addon/__init__.py`（Anki 外掛用，不能 import core，故各寫一份）各維護一套。association 一路串進造句入口。

**Tech Stack:** Python, Groq/Ollama LLM, pytest（僅測 core 的 prompt 組裝，不呼叫真 LLM；addon 以 py_compile + 手動驗證）。

參考設計：`docs/superpowers/specs/2026-06-10-example-sentence-sense-redesign-design.md`

---

## 重要前提

- **不能 import 的兩份**：`addon/__init__.py` 跑在 Anki 的 Python，不能 import `core/`。所以 prompt 邏輯 addon 與 core **各寫一份、各自維護**（CLAUDE.md 既有踩雷點）。本計畫兩邊都改。
- **LLM 輸出品質無法用單元測試斷言**：自動測試只驗「prompt 是否含正確指令」（patch `llm()` 攔截 prompt）。句子是否真的變短變清楚、SWE 語意是否正確 → **靠重啟 Anki / 跑 CLI 手動驗證**（Task 8）。
- **既有卡不受影響**：只改生成邏輯，舊卡需手動重生才會套用。

---

## Task 1: core — 共用造句指令 `_sentence_instructions` + 改 `llm_sentence`

**Files:**
- Modify: `core/llm.py`（`llm_sentence`，約 :94-96；在其前新增 `_sentence_instructions`）
- Test: `test_llm_prompts.py`（新建）

- [ ] **Step 1: 先寫測試（捕捉 prompt 內容）**

新建 `test_llm_prompts.py`：

```python
"""core.llm prompt-construction tests — verify meaning-selection + quality rules are
present in the prompts, WITHOUT calling the real LLM (patch the llm() dispatcher)."""
from unittest.mock import patch
import core.llm as llm_mod


def _capture(fn, *args):
    """Call fn with llm() patched to record the prompt and return a valid 2-line reply."""
    seen = {}
    def fake_llm(prompt):
        seen["prompt"] = prompt
        return "A developer follows the team's naming convention here.\nnaming convention code screen"
    with patch.object(llm_mod, "llm", fake_llm):
        fn(*args)
    return seen["prompt"]


class TestSentencePrompt:
    def test_includes_swe_then_everyday_priority(self):
        p = _capture(llm_mod.llm_sentence, "convention")
        assert "software engineering" in p
        assert "everyday meaning" in p

    def test_bans_definition_or_circular_sentence(self):
        p = _capture(llm_mod.llm_sentence, "convention")
        assert "Do NOT write a definition" in p
        assert "X means" in p

    def test_uses_association_hint_when_given(self):
        p = _capture(llm_mod.llm_sentence, "convention", "coding standard")
        assert "coding standard" in p
        assert "Hint:" in p

    def test_no_hint_line_without_association(self):
        p = _capture(llm_mod.llm_sentence, "convention")
        assert "Hint:" not in p
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest test_llm_prompts.py::TestSentencePrompt -v`
Expected: FAIL（`llm_sentence` 仍是舊 prompt，缺 "software engineering" 等字串；且 `llm_sentence("convention","coding standard")` 會 TypeError，因舊簽名只收 word）

- [ ] **Step 3: 實作 `_sentence_instructions` 並改寫 `llm_sentence`**

在 `core/llm.py` 中 `llm_sentence` 之前新增，並替換 `llm_sentence`：

```python
def _sentence_instructions(word, association=""):
    """Shared meaning-selection + sentence-quality rules for example-sentence prompts.
    Priority: hint (association) > software-engineering sense > most common everyday sense."""
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
        f'sentence alone. Keep it as short and simple as you can WITHOUT losing that clarity: '
        f'shorter is better, but a clear sentence always beats a short unclear one. '
        f'Use plain, everyday language; avoid business/corporate phrasing and complex clauses. '
        f'A software / code-flavoured situation is fine. '
        f'Do NOT write a definition or a circular sentence (no "X means ...", "X is when ...", '
        f'"{word} is a kind of ..."); show the meaning through a real, concrete situation.'
    )


def llm_sentence(word, association=""):
    prompt = _sentence_instructions(word, association) + "\n\nOutput only the sentence. No explanation, no quotes."
    result = llm(prompt)
    return result if len(result) > 10 else ""
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest test_llm_prompts.py::TestSentencePrompt -v`
Expected: PASS（4 個）

- [ ] **Step 5: Commit**

```bash
git add core/llm.py test_llm_prompts.py
git commit -m "feat: core 造句 prompt 鎖語意（association>SWE>日常）+ 防呆禁定義句"
```

---

## Task 2: core — `llm_sentence_and_query` 改用共用指令（`definition`→`association`）

**Files:**
- Modify: `core/llm.py`（`llm_sentence_and_query`，約 :105-131）
- Modify: `backfill_words.py:82`（呼叫端參數名）
- Test: `test_llm_prompts.py`

- [ ] **Step 1: 加測試**

在 `test_llm_prompts.py` 末尾加：

```python
class TestSentenceAndQueryPrompt:
    def test_uses_shared_instructions_and_two_lines(self):
        p = _capture(llm_mod.llm_sentence_and_query, "thread", "execution unit")
        assert "software engineering" in p           # shared instructions present
        assert "execution unit" in p                 # association threaded as hint
        assert "Line 1" in p and "Line 2" in p       # still asks for sentence + image query
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest test_llm_prompts.py::TestSentenceAndQueryPrompt -v`
Expected: FAIL（舊 prompt 無 "software engineering"；且舊參數名為 `definition`，`_capture` 以位置參數傳入仍可跑，但缺指令字串 → 斷言失敗）

- [ ] **Step 3: 改寫 `llm_sentence_and_query`**

```python
def llm_sentence_and_query(word, association="", sentence=""):
    extra = f'\n(There is already an example sentence; keep the SAME meaning: "{sentence}")' if sentence else ""
    prompt = (
        _sentence_instructions(word, association) + extra +
        "\n\nProvide exactly two lines:\n"
        "Line 1: the example sentence.\n"
        "Line 2: a 5-8 word Google image search query for a photo that visually represents "
        "the meaning you used.\n\n"
        "Output only the two lines, nothing else. No labels, no numbering."
    )
    result = llm(prompt)
    lines = [l.strip() for l in result.strip().splitlines() if l.strip()]
    if len(lines) >= 2:
        sent = lines[0].strip('"\'')
        query = lines[1].strip('"\'')
        if len(sent) > 10:
            return sent, query
    if len(lines) == 1 and len(lines[0]) > 10:
        return lines[0].strip('"\''), f"{word} {association} photo" if association else f"{word} illustration"
    return "", f"{word} {association} photo" if association else f"{word} illustration"
```

- [ ] **Step 4: 更新呼叫端 `backfill_words.py:82`**

把：
```python
        sentence, img_query = llm_sentence_and_query(word, definition=current_assoc, sentence=current_sentence)
```
改成：
```python
        sentence, img_query = llm_sentence_and_query(word, association=current_assoc, sentence=current_sentence)
```

- [ ] **Step 5: 跑測試確認通過**

Run: `uv run pytest test_llm_prompts.py -v`
Expected: PASS（全部）

- [ ] **Step 6: Commit**

```bash
git add core/llm.py backfill_words.py test_llm_prompts.py
git commit -m "feat: core llm_sentence_and_query 共用造句指令；definition→association"
```

---

## Task 3: core — `llm_translate` 跟句意 + 去重 + 鬆綁字數

**Files:**
- Modify: `core/llm.py`（`llm_translate`，約 :99-102）
- Test: `test_llm_prompts.py`

- [ ] **Step 1: 加測試**

在 `test_llm_prompts.py` 末尾加：

```python
class TestTranslatePrompt:
    def test_follows_sentence_sense(self):
        p = _capture(llm_mod.llm_translate, "convention", "We follow a naming convention.")
        assert "as it is used in this sentence" in p
        assert "naming convention" in p

    def test_bans_synonym_lists(self):
        p = _capture(llm_mod.llm_translate, "cup", "She filled the cup with tea.")
        assert "near-duplicate" in p
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest test_llm_prompts.py::TestTranslatePrompt -v`
Expected: FAIL（舊 prompt 用 "Translate the English word ... 1-4 characters"，無 "as it is used in this sentence" / "near-duplicate"）

- [ ] **Step 3: 改寫 `llm_translate`**

```python
def llm_translate(word, sentence=""):
    ctx = f' as it is used in this sentence: "{sentence}"' if sentence else ""
    result = llm(
        f'Give the Traditional Chinese meaning of "{word}"{ctx}. '
        f'Give ONE concise translation only — do NOT list synonyms or near-duplicate terms '
        f'(e.g. never "水杯、茶杯"). Keep it short (usually 1-4 characters; a little longer only '
        f'if a single term genuinely needs it). Output only the Chinese, no explanation.'
    )
    return result.strip() if result else ""
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest test_llm_prompts.py -v`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add core/llm.py test_llm_prompts.py
git commit -m "feat: core llm_translate 跟句意翻譯 + 禁列近義詞 + 鬆綁字數"
```

---

## Task 4: addon — 模組級 `_sentence_prompt` + 改 `_groq_sentence` / `_ollama_sentence` / `_llm_sentence`

**Files:**
- Modify: `addon/__init__.py`（`_groq_sentence` :256-258、`_ollama_sentence` :260-、`_llm_sentence` :277-284；在 class 外新增 `_sentence_prompt`）

> addon 不能被 pytest import（需要 `aqt`），本 Task 以 `py_compile` 驗證語法，行為留待 Task 8 手動驗證。

- [ ] **Step 1: 新增模組級 `_sentence_prompt`**

在 `addon/__init__.py` 適當位置（例如 `_looks_english` 附近的模組函式區）新增：

```python
def _sentence_prompt(word, association=""):
    """Example-sentence prompt: pick sense (hint > SWE > everyday), short & clear, no
    definition/circular sentence. Mirror of core.llm._sentence_instructions (addon
    cannot import core)."""
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
        f'sentence alone. Keep it as short and simple as you can WITHOUT losing that clarity: '
        f'shorter is better, but a clear sentence always beats a short unclear one. '
        f'Use plain, everyday language; avoid business/corporate phrasing and complex clauses. '
        f'A software / code-flavoured situation is fine. '
        f'Do NOT write a definition or a circular sentence (no "X means ...", "X is when ...", '
        f'"{word} is a kind of ..."); show the meaning through a real, concrete situation.\n\n'
        f'Output only the sentence. No explanation, no quotes.'
    )
```

- [ ] **Step 2: 改 `_groq_sentence`（加 association）**

```python
    def _groq_sentence(self, word, association=""):
        return _groq_chat(_sentence_prompt(word, association), temperature=0.7, max_tokens=200, timeout=15)
```

- [ ] **Step 3: 改 `_ollama_sentence`（加 association）**

把 payload 內的 `"prompt": f'Write one short...'` 改為 `"prompt": _sentence_prompt(word, association),`，並把方法簽名改成 `def _ollama_sentence(self, word, association=""):`。

- [ ] **Step 4: 改 `_llm_sentence`（傳遞 association）**

```python
    def _llm_sentence(self, word, association=""):
        result = self._groq_sentence(word, association)
        if result and len(result) > 10:
            return result, "Groq"
        result = self._ollama_sentence(word, association)
        if result and len(result) > 10:
            return result, "Ollama"
        return "", "failed"
```

- [ ] **Step 5: 語法檢查**

Run: `uv run python -m py_compile addon/__init__.py && echo OK`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add addon/__init__.py
git commit -m "feat: addon 造句 prompt 鎖語意 + 加 association 參數（_sentence_prompt）"
```

---

## Task 5: addon — 串接 association 到造句呼叫端（⌘D 與 ⌘S）

**Files:**
- Modify: `addon/__init__.py`（`Worker._process` 約 :195；`BackfillWorker._process_one` 約 :640）

- [ ] **Step 1: ⌘D — `Worker._process` 傳 association**

把：
```python
            sentence, engine = self._llm_sentence(word)
```
改成：
```python
            sentence, engine = self._llm_sentence(word, self.association)
```
（`self.association` 是 AddWordDialog 傳入 Worker 的關聯字，已存在。）

- [ ] **Step 2: ⌘S — `BackfillWorker._process_one` 取卡片 Association 並傳入**

在 `_process_one` 內、決定 `sentence` 的區塊之前，取出 association（note 的 fields 已含 Association，見 `_scan` payload）：

```python
        assoc = _clean_text(note["fields"].get("Association", {}).get("value", ""))
```
再把：
```python
            sentence, _ = self._w._llm_sentence(word)
```
改成：
```python
            sentence, _ = self._w._llm_sentence(word, assoc)
```

- [ ] **Step 3: 語法檢查**

Run: `uv run python -m py_compile addon/__init__.py && echo OK`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add addon/__init__.py
git commit -m "feat: addon ⌘D/⌘S 造句串接 association（不再瞎猜語意）"
```

---

## Task 6: addon — `_groq_translate` 跟句意 + 去重 + 鬆綁字數（含驗證門檻）

**Files:**
- Modify: `addon/__init__.py`（`_groq_translate` :286-297）

- [ ] **Step 1: 改 prompt 與字數驗證門檻**

把現有：
```python
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
```
改成：
```python
    def _groq_translate(self, word, sentence):
        """Traditional Chinese meaning of word AS USED IN the sentence. '' on failure."""
        prompt = (f'Give the Traditional Chinese meaning of "{word}" as it is used in this '
                  f'sentence: "{sentence}". Give ONE concise translation only — do NOT list '
                  f'synonyms or near-duplicate terms (e.g. never "水杯、茶杯"). Keep it short '
                  f'(usually 1-4 characters; a little longer only if a single term genuinely '
                  f'needs it). Output only the Chinese, no explanation.')
        reply = _groq_chat(prompt, temperature=0.3, max_tokens=20, timeout=10)
        # reject implausible output → leave empty so ⌘S re-generates it later
        if re.search(r"[A-Za-z]", reply):                    # English preamble / refusal / paren
            return ""
        if len(re.findall(r"[一-鿿]", reply)) > 8:   # >8 漢字 = a sentence, not a single term
            return ""
        return reply
```
（門檻 6→8：鬆綁後單一詞可能略長，避免合法詞被誤殺；仍擋整句。）

- [ ] **Step 2: 語法檢查**

Run: `uv run python -m py_compile addon/__init__.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add addon/__init__.py
git commit -m "feat: addon _groq_translate 跟句意 + 禁近義詞 + 字數門檻 6→8"
```

---

## Task 7: CLI — `add_word.py` 傳 association 給造句

**Files:**
- Modify: `add_word.py:107`

- [ ] **Step 1: 傳入 association**

把：
```python
    sentence = llm_sentence(word) or f"Please add an example sentence for '{word}'."
```
改成：
```python
    sentence = llm_sentence(word, association) or f"Please add an example sentence for '{word}'."
```
（`association` 變數在 :85 已取得。）

- [ ] **Step 2: 語法檢查**

Run: `uv run python -m py_compile add_word.py && echo OK`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add add_word.py
git commit -m "feat: add_word.py 造句串接 association"
```

---

## Task 8: 手動驗證（Anki + CLI）

> LLM 句子品質無法自動斷言，這步用真實生成驗收 spec。

- [ ] **Step 1: 重啟 Anki**（⌘Q 完全結束再開，讓 addon 重新載入）

- [ ] **Step 2: ⌘D 測多義 / SWE 字（不給 association）**

逐一新增並檢查：`convention`、`thread`、`payload`、`throttle`
Expected：
- 句子明顯**比以前短、白話**，看得出字義；**非**「X means…」式定義句。
- 走 **SWE 語意**（convention→命名慣例情境、thread→執行緒、payload→資料酬載、throttle→限流）。
- 單字 Meaning 與例句**同義**（convention→慣例/規範，不再是會議），且**不列重複近義詞**。
- 圖與句子吻合。

- [ ] **Step 3: ⌘D 測純日常字**：`garden`、`weather` → 應退回日常義。

- [ ] **Step 4: ⌘D 測有 association 的字**：例如 word=`spring`、association=`coil metal`（或 `season`）→ 句子語意應跟著 association，而非預設 SWE/日常。

- [ ] **Step 5: ⌘S 測手機/內建新增的卡**：找一張有 Association、缺例句的卡，⌘S 補完 → 句子應有用到該 Association 的語意。

- [ ] **Step 6: CLI 測**：
```bash
uv run python add_word.py "idempotent" "same result on retry"
```
Expected：句子用「重試結果相同」這個語意（SWE/association），短且清楚。

- [ ] **Step 7（若有不符）**：回報哪個字、哪裡不符（太長 / 定義句 / 語意錯 / 列近義詞），據此微調 `_sentence_instructions` / `_sentence_prompt` / 翻譯 prompt 的措辭，重跑本 Task。

---

## Task 9: 文件同步（CLAUDE.md 踩雷點）

**Files:**
- Modify: `CLAUDE.md`（Key Rules 區）

- [ ] **Step 1: 新增/更新踩雷點**

在 CLAUDE.md「Key Rules」加一條（措辭可調）：

```
- 例句/單字翻譯的「語意」由造句 prompt 決定，優先序：Association → SWE 領域義 → 常用日常義；單字翻譯 `_groq_translate`/`llm_translate` 一律「依句中用法」翻、且禁列近義重複詞。改 prompt 要**同時改 addon `_sentence_prompt` 與 core `_sentence_instructions` 兩份**（addon 不能 import core）。association 已串進 ⌘D/⌘S/CLI 造句，別再讓它只餵圖片。
```

- [ ] **Step 2: README 視需要**

README 不寫 prompt 內部；若要讓使用者知道「例句偏 SWE 語意」，可在用法區加一句註記（可選，非必要）。

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md README.md
git commit -m "docs: 記錄例句語意優先序與 addon/core 兩份 prompt 同步規則"
```

---

## Self-Review（plan vs spec）

- spec 3.1 語意優先序 → Task 1（`_sentence_instructions`）、Task 4（`_sentence_prompt`）✓
- spec 3.2 短/清楚/防呆 → 同上 prompt 內含 ✓
- spec 3.3 順序 sentence→image→audio → 現況已如此（image 吃 sentence），未改動破壞；Task 8 驗證 ✓
- spec 3.4 association 優先 → prompt 第 1 順位 + Task 5/7 串接呼叫端 ✓
- spec 4.3 翻譯跟句意 + 去重 + 鬆綁 + 門檻 → Task 3（core）、Task 6（addon）✓
- spec 4.6 所有位置 → core（T1/2/3）、addon prompt（T4）、addon 呼叫端（T5）、addon 翻譯（T6）、add_word（T7）、backfill_words（T2 Step4）✓
- 兩份同步（addon/core）→ T1 vs T4、T3 vs T6 對照 ✓；CLAUDE.md 記錄（T9）✓
- 型別/簽名一致：`_sentence_instructions(word, association="")`(core) / `_sentence_prompt(word, association="")`(addon)；`_llm_sentence(word, association="")`；`llm_sentence(word, association="")`；`llm_sentence_and_query(word, association="", sentence="")`；`llm_translate(word, sentence="")` 不變簽名只改 prompt ✓
