# 例句語意品質重構：White Method 對齊 + SWE 領域優先

日期：2026-06-10
狀態：設計中（待使用者覆核）

## 1. 背景與問題

目前自動生成的例句品質不佳：
- 偏「企業 / 公司」敘述腔，又長又難，每個字都長一個樣。
- 沒有把「這個單字是什麼意思、為什麼這樣用」講清楚 —— 學習者看完句子仍不懂字義。
- 多義字（如 `convention`）會隨機挑語意（生成「科幻展會」），導致單字翻譯（會議）、整句翻譯（展會）、例句三者各說各話。

過去的修法方向（讓翻譯彼此一致）是**治標且方向錯誤**：真正的病灶在「例句沒鎖定正確語意、且太難」。

## 2. 核心價值（修正後的理解）

懷特學習法（White Method）的核心是 **用圖片 + 英文句子快速學單字**。翻譯欄位並非此法的一環，是因為句子太難才不得已加上的「拐杖」。因此：
- 重點是**例句要短、簡單、精準傳達字義**，搭配吻合的圖與音。
- 翻譯是輔助，只需與例句語意一致即可（本次不討論移除）。
- **工程量不是考量** —— 以「語意正確、貼合學習者」為最高目標。

## 3. 需求

### 3.1 語意決定（優先序）
對每個單字，決定要教哪個語意，依序：
1. **有 Association（提示）** → 用提示指向的語意。
2. **否則，若該字有軟體工程 / 程式 / 技術領域的常見用法** → 一律優先用該 SWE 語意（即使日常更常用別的意思）。例：`convention`→命名慣例/規範、`thread`→執行緒、`payload`→酬載。
3. **都沒有** → 退回最常用的日常義。

（使用者為 software engineer，全牌組向其專業靠攏，可提升情境相關性與記憶效果。）

### 3.2 例句
- **「清楚」優先於「短」**：在不犧牲清楚的前提下，盡可能用最精簡的句子表達字義。寧可長一點也要讓人看懂，不可為了短而句意不明。**不設硬性字數上限**（寫死字數會逼出爛句）。
- **精準表達字義**：不懂這個字的人，光看句子就能猜出意思。
- **防呆**：不可寫定義式 / 循環句（如 `"X means ..."`、`"X is when ..."`、`"{word} is a kind of ..."`），必須用真實、具體的情境自然帶出字義。
- 用平實日常語言，**避免企業 / 商業腔與複雜子句**。
- 句子情境**可以技術 / 程式碼導向**（使用者為 SWE，看得懂也樂見），不必刻意日常化。

### 3.3 順序
- **先 sentence，再依 sentence 生 image 與 audio**，確保三者吻合。

### 3.4 Association
- 有 Association 時**優先拿它輔助造句**（見 3.1 第 1 點），不要自己瞎猜語意。

## 4. 設計

### 4.1 語意決定放在「造句 prompt 內」完成（單次呼叫）
不另開一個獨立的「分類 / 過濾」API 步驟，而是在造句 prompt 內讓模型依 3.1 的優先序自行判斷語意後直接造句。
- 理由：一次呼叫即可完成「判斷語意 + 造句」，不增加 ⌘D 即時新增的延遲與速率壓力；模型有能力在 prompt 內完成此判斷。
- （替代方案：先呼叫一次做 SWE 語意分類，再造句 —— 一致性可能略強，但每張卡多一次呼叫、流程變複雜。本設計不採用，除非實測品質不足。）

### 4.2 新的「造句」prompt（草稿，addon 與 core 共用同一套措辭）

```
You are helping a software engineer learn the English word "{word}".

Pick the meaning to teach, in this priority:
1. If a hint is given, use the sense the hint points to. Hint: "{association}"
2. Else if "{word}" has a common usage in software engineering / programming / tech,
   use that sense.
3. Else use its most common everyday meaning.

Then write ONE example sentence that uses "{word}" naturally and makes its meaning
obvious — someone who doesn't know the word should be able to guess it from the
sentence alone. Keep it as short and simple as you can WITHOUT losing that clarity:
shorter is better, but a clear sentence always beats a short unclear one.
Use plain, everyday language; avoid business/corporate phrasing and complex clauses.
A software / code-flavoured situation is fine.
Do NOT write a definition or a circular sentence (no "X means ...", "X is when ...",
"{word} is a kind of ..."); show the meaning through a real, concrete situation.

Output only the sentence. No explanation, no quotes.
```
- 無 association 時，省略第 1 點（或標明 "no hint given"）。
- **不做硬性字數上限**：以 prompt 引導「清楚優先、盡量精簡」，不程式截斷（截斷會破壞語法），也不寫死字數（會逼出爛句）。
- 防呆禁止定義句 / 循環句，強制用具體情境自然帶出字義。

### 4.3 新的「單字翻譯」prompt（跟著句子語意，確保一致）

```
Give the Traditional Chinese meaning of "{word}" as it is used in this sentence:
"{sentence}". Give ONE concise translation only — do NOT list synonyms or
near-duplicate terms (e.g. never "水杯、茶杯"). Keep it short (usually 1-4 characters;
a little longer only if a single term genuinely needs it). Output only the Chinese,
no explanation.
```
- 由於句子已鎖定（提示 / SWE / 日常）語意，依「句中用法」翻譯即與例句一致。
- **鬆綁字數**：從硬性「1-4 字」改為「通常 1-4、單一詞真的需要才略長」。
- **去重**：只給一個精簡譯法，禁止列近義 / 重複詞（如「水杯、茶杯」）。
- 既有輸出驗證（拒英文前言）保留；「>6 漢字＝句子」的門檻因單一詞極少超過、且鬆綁後仍需擋整句，**改為 >8 漢字**才判定為句子。

### 4.4 整句翻譯與圖片
- 整句翻譯 `*_translate_sentence` prompt **不需改**（本來就是翻當下那句）。
- 圖片本來就吃 word + association + sentence，句子語意修正後圖自然跟著吻合；順序維持 sentence→image→audio。

### 4.5 Association 接進造句（目前的落差，需補齊）
現況：
- `add_word.py` 用 `llm_sentence(word)` → **沒帶 association**（雖然 association 當下就有）。
- addon `_groq_sentence(word)` / `_ollama_sentence(word)` / `_llm_sentence(word)` → **沒帶 association**（⌘D 與 ⌘S 兩條路都缺）。
- `backfill_words.py` 用 `llm_sentence_and_query(word, definition=current_assoc, ...)` → **已經有帶**（行為與其他路不一致）。

設計：所有造句入口統一接受並使用 association（無則空字串），行為一致。

### 4.6 要改的所有位置（addon 與 core 各一份，因 addon 不能 import core）

**例句 prompt / 簽名：**
- `addon/__init__.py`：`_groq_sentence`、`_ollama_sentence`（加 association 參數 + 新 prompt）；`_llm_sentence`（傳遞 association）。
- 呼叫端：`Worker._process`（⌘D，傳 `self.association`）、`BackfillWorker._process_one`（⌘S，傳卡片的 Association 欄位）。
- `core/llm.py`：`llm_sentence`（加 association + 新 prompt）；`llm_sentence_and_query`（對齊新 prompt 方向）。
- 呼叫端：`add_word.py:107`（傳 association）、`backfill_words.py:82`（已傳，確認對齊新措辭）。

**單字翻譯 prompt：**
- `addon/__init__.py`：`_groq_translate`。
- `core/llm.py`：`llm_translate`。

## 5. 不在範圍（Out of scope）
- 不移除翻譯欄位（雖非 White Method 一環，本次保留）。
- 不自動重生既有卡片（見第 7 節）。
- 整句翻譯 prompt 不改。
- 卡片版面 / CSS 不動。

## 6. 驗證方式
重啟 Anki 後，用涵蓋不同情況的字實測 ⌘D 新增：
- **有 SWE 義的多義字**：`convention`（預期句子用「命名慣例/規範」義，單字翻譯≈慣例/規範，圖與句吻合）、`thread`、`payload`、`throttle`。
- **純日常字（無 SWE 義）**：例如 `garden`、`weather` → 退回日常義。
- **有給 association 的字**：association 指向的語意應勝過 SWE / 日常。
- 句子應明顯比現在**更短、更白話**，且能讓人從句子猜出字義。
- ⌘S 補例句的卡片同樣套用（含 association）。
- CLI：`add_word.py`、`backfill_words.py` 行為與 addon 一致。

## 7. 待確認 / 風險
- **既有卡不自動修好**：⌘S 只補空欄位，已有例句/翻譯的舊卡不會重生。若要套用到既有卡，需另外決定「批次重生例句」機制（本次不做，可列後續）。
- **句長靠「清楚優先、盡量精簡」原則 + 防呆**：不設硬上限。若實測仍偏長或出現定義式句子，再加強 prompt（不走程式截斷）。
- **SWE 判斷靠模型 inline**：若實測語意判斷不穩，再評估 4.1 的「獨立分類步驟」替代方案。
