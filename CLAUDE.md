# Anki White Forge

Anki 自動化單字系統，牌組 `My_Daily_English`、筆記類型 `English_White_Method`。

> **完整介紹、安裝、架構、欄位、用法 → 見 [README.md](README.md)（使用面資訊的單一事實來源）。**
> 本檔只寫 README 不涵蓋的東西：給 AI（Claude）改 code 用的規則與踩雷點。

## 文件分工：什麼寫 README、什麼寫 CLAUDE.md

判斷準則一句話：**「給人理解 / 使用專案」→ README；「避免 AI 改壞 code」→ CLAUDE.md。**

- **README.md**（給人 / 開源使用者看）：專案介紹、安裝步驟、日常用法、架構總覽、牌組欄位、快捷鍵、CLI、配色設計、FAQ。**使用面資訊的單一事實來源。**
- **CLAUDE.md**（給 Claude / 維護者看）：README 不會寫的隱性知識 —— 改 code 的踩雷點、架構約束、commit / pre-push 流程。需要引用使用面資訊時寫「→ 見 README」，**不複製內容**。

**鐵則：同一個事實只放一處。** 兩邊重複遲早會不同步（drift）——例如快捷鍵曾經 README、CLAUDE.md、code 三處各說各話，最後 README 是錯的。

## Key Rules（改 code 踩雷點）

- Addon 真檔在 repo `addon/`，symlink 到 Anki `addons21/my_word_adder/`；改完 `addon/` 需**重啟 Anki** 才生效
- Addon（`addon/__init__.py`）跑在 Anki 的 Python，**不能 import `core/`** → 改用 subprocess 呼叫 `_image_helper.py`、`_gtts_helper.py`、`_validate_helper.py`
- Addon 用 urllib 直呼 Groq（非 groq SDK）→ 一定要帶 `User-Agent: AnkiWordAdder/1.0` header
- 圖片偵測用 `"<img" in value`（不是 `bool(value)`），以處理殘留 HTML
- `backfill_words.py` 在句子變動時會重生音檔（`need_sentence` flag）
- ⌘D（Add）和 ⌘S（Complete）都會生成全部欄位含 `Translation`，共用 `Worker._groq_translate()`
- 非英文字元用共用 `_looks_english()` 擋：⌘D 建立前擋、⌘S 掃描時略過非英文卡片（手機/Anki 內建新增繞過 ⌘D，故 ⌘S 是最後關卡 → 驗證要兩邊都做、邏輯共用）
- ⌘D 拼字另用 Groq `_groq_spellcheck()`（回 OK／更正字／NONWORD），斷網退 `_validate_helper.py` 離線拼字
- `Sentence_CN`（整句中文翻譯）**只由 ⌘D（即時）與專用選單「Backfill Sentence Translations」/ CLI `backfill_sentence_cn.py` 填**；**⌘S Complete 與 `backfill_words.py` 刻意不碰**（否則「補全部欄位」會對全牌組觸發未節流大量翻譯而撞速率上限）。翻譯：core `llm_translate_sentence` / addon `_groq_translate_sentence` 各寫一份（addon 不能 import core），驗證以「含中文且英文詞 < 3」判定 → 保留嵌入英文詞（concurrency、Microsoft）的合法譯文
- **卡片模板裡「可點」的元件一律用 `<button>`/`<a>`，不要用 `<div>`** —— AnkiMobile 原生 tap 手勢會略過互動元件；用 `<div>`+JS `stopPropagation` 擋不住原生手勢（點擊會被當成翻牌/評分），且卡片 `<script>` 跑幾張後 AnkiMobile 會停止重跑。見 `templates/back.html` 的 `.trans-box`（翻譯框）
- 大量翻譯走 pacing（撞 429 就等 `Retry-After` 再續，不猜固定批量）。偵測 429：core `groq_generate_strict` 拋 `RateLimitReached`、addon `_groq_chat_strict` 拋 `_AddonRateLimited`，各帶 `retry_after`

## Git 規則

- 每完成一個功能或修復就立即 commit，不累積多個功能到同一 commit
- 一個 commit 只做一件事：一個 bug fix / feature / refactor
- commit type 用 conventional 風格：`feat` / `fix` / `docs` / `style` / `refactor` / `perf` / `chore` 等
- commit 前先跑 Pre-push Checklist
- 不確定要不要 commit 時，問使用者

## Running

所有 script：`uv run python <script>.py`，需 Anki 開著並啟用 AnkiConnect。模板部署：`uv run python update_template.py`。

## Pre-push Checklist

1. README.md 是否同步更新（使用面資訊有變動？）
2. CLAUDE.md 是否同步更新（踩雷點 / 規則有變動？）
3. Addon 改動在 `addon/`（symlink 到 Anki），改完重啟 Anki 驗證
4. 同一事實沒有同時寫進 README 和 CLAUDE.md（無重複、無 drift）
