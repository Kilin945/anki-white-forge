# Anki White Forge

Anki 自動化單字系統，牌組 `My Daily English`、筆記類型 `English_White_Method`。

> **完整介紹、安裝、架構、欄位、用法 → 見 [README.md](README.md)（使用面資訊的單一事實來源）。**
> 本檔只寫 README 不涵蓋的東西：給 AI（Claude）改 code 用的規則與踩雷點。

## 文件分工：什麼寫 README、什麼寫 CLAUDE.md

判斷準則一句話：**「給人理解 / 使用專案」→ README；「避免 AI 改壞 code」→ CLAUDE.md。**

- **README.md**（給人 / 開源使用者看）：專案介紹、安裝步驟、日常用法、架構總覽、牌組欄位、快捷鍵、CLI、配色設計、FAQ。**使用面資訊的單一事實來源。**
- **CLAUDE.md**（給 Claude / 維護者看）：README 不會寫的隱性知識 —— 改 code 的踩雷點、架構約束、commit / pre-push 流程。需要引用使用面資訊時寫「→ 見 README」，**不複製內容**。

**鐵則：同一個事實只放一處。** 兩邊重複遲早會不同步（drift）——例如快捷鍵曾經 README、CLAUDE.md、code 三處各說各話，最後 README 是錯的。

**README 行文規則：短句、一句一事。** 不用箭頭鏈（A → B → C）、不用斜線串接（例句／翻譯／圖）、不把括號套進括號。流程寫成編號步驟或列點。改功能後補文件時照這個風格寫，不要把新資訊壓回一行——資訊全對但密到要回頭解析，一樣是爛文件（2026-08-27 整份 README 因此重寫過一次）。

## Key Rules（改 code 踩雷點）

- Addon 真檔在 repo `addon/`，Anki 端 `addons21/my_word_adder` 是**指向整個 `addon/` 資料夾的 symlink**（新增檔案自動生效，不用補 link；Anki 會把 `meta.json`/`__pycache__` 寫進 repo `addon/`，已 gitignore）；改完 `addon/` 需**重啟 Anki** 才生效
- Addon（`addon/__init__.py`）跑在 Anki 的 Python，**不能 import `core/`** → 改用 subprocess 呼叫 `_image_helper.py`、`_gtts_helper.py`、`_validate_helper.py`
- Addon 的 LLM 呼叫走 `addon/_llm_dispatch.py`（urllib、非 SDK；Groq 必帶 `User-Agent: AnkiWordAdder/1.0`）
- 圖片偵測用 `"<img" in value`（不是 `bool(value)`），以處理殘留 HTML
- `backfill_words.py` 在句子變動時會重生音檔（`need_sentence` flag）
- ⌘A（Add）和 ⌘S（Complete）都會生成全部欄位含 `Translation`，共用 `Worker._groq_translate()`
- ⌘S 按 Complete 時 `_on_run` 會用 `_note_snapshot()` **重新讀最新欄位**（不是 `_pending_notes` 這種開窗時的快照）；佔位符**絕不覆蓋非空真句子**（生成失敗時該不該寫佔位符是純函式 `_sentence_to_write`，None＝保留原句）——事故根因：舊版用開窗快照判斷「還缺什麼」，撞限重跑會把已生成的真句子蓋成佔位符。停批政策：`_dispatcher.wall_secs()` 量到的牆 ≤ `SHORT_WALL_WAIT`(2s) 就原地等掉續跑，更長才停批回報（雙 provider 常見秒級小牆，不值得整批中止）。批次/LLM 事件（failover、429、斷路器 OPEN、撞限停批、佔位符守門觸發）集中記錄到 `logs/addon_llm.log`（已 gitignore，`RotatingFileHandler` 1MB×3 輪替，經 `_lld.get_logger()`）。句音由 `_need_sentence_audio` 決定：佔位符不配音、句子重寫強制重生
- **句子不可用（空/佔位符）→ 依賴句意的下游全部跳過**（Image、Translation、Sentence_CN、句音；Front_Audio 與句子無關照做），亮橘等下次 ⌘S 連同句子一起重做——事故根因：句子生成失敗仍照跑下游，`Sentence_CN` 變成「佔位符的翻譯」這種欄位彼此不一致的髒卡。判斷是純函式 addon `_sentence_usable` / core `sentence_usable`（KEEP-IN-SYNC），閘門接在 **⌘A `Worker.run`、⌘S `_process_one`、CLI `backfill_words.py`** 三處，改一處要檢查另兩處
- LLM 呼叫的 `effort` 參數（預設 `"low"`）：**造句傳 `"medium"`**（多條件約束任務），翻譯/拼字等查詢式短呼叫維持 low。對映：Groq `reasoning_effort` 直吃；Gemini 只有 low/high 兩檔（minimal 被 API 拒）→ low 以上一律 high。思考餘裕跟著走：low=`REASONING_HEADROOM`(512)、更高=`REASONING_HEADROOM_DEEP`(1024)，兩份 KEEP-IN-SYNC
- 非英文字元用共用 `_looks_english()` 擋：⌘A 建立前擋、⌘S 掃描時略過非英文卡片（手機/Anki 內建新增繞過 ⌘A，故 ⌘S 是最後關卡 → 驗證要兩邊都做、邏輯共用）
- ⌘A 拼字另用 Groq `_groq_spellcheck()`（回 OK／更正字／NONWORD），斷網退 `_validate_helper.py` 離線拼字
- `Sentence_CN`（整句中文翻譯）由 **⌘A（即時）、⌘S Complete（日常少量補完，只翻當下缺的幾張 → 不會撞速率）、Batch Operations 面板的「Backfill Sentence Translations」區塊 / `⌘F` / CLI `backfill_sentence_cn.py`（大量、節流）** 填。**CLI `backfill_words.py` 仍刻意不碰**（它是未節流的大量補齊，整句翻譯量大會撞速率上限 → 大量場景一律走 `backfill_sentence_cn.py`）。⌘S 與 backfill_words.py 的差異就在這：GUI ⌘S 補（量小、互動），CLI 大量補齊不補。翻譯：core `llm_translate_sentence` / addon `_groq_translate_sentence` 各寫一份（addon 不能 import core），驗證以「含中文且英文詞 < 3」判定 → 保留嵌入英文詞（concurrency、Microsoft）的合法譯文
- **卡片模板裡「可點」的元件一律用 `<button>`/`<a>`，不要用 `<div>`** —— AnkiMobile 原生 tap 手勢會略過互動元件；用 `<div>`+JS `stopPropagation` 擋不住原生手勢（點擊會被當成翻牌/評分），且卡片 `<script>` 跑幾張後 AnkiMobile 會停止重跑。見 `templates/back.html` 的 `.trans-box`（翻譯框）
- 大量翻譯走 pacing（撞 429 就等 `Retry-After` 再續，不猜固定批量）。偵測 429：core `groq_generate_strict` 拋 `RateLimitReached`、addon `_groq_chat(strict=True)` 在兩家 provider 都不可用（`AllProvidersLimited`）時拋 `_AddonRateLimited`，各帶 `retry_after`。連續 429 走指數退避（`_backoff_secs`，上限 60s，兩份 KEEP-IN-SYNC）——別輕信 Retry-After（Groq 免費層 TPM 耗盡仍回 2s）
- 例句與單字翻譯的「語意」由**造句 prompt** 決定，優先序：**Association（提示）→ SWE 領域義 → 常用日常義**；單字翻譯（`_groq_translate` / `llm_translate`）一律「依句中用法」翻、且**禁列近義重複詞**（如「水杯、茶杯」）。造句 prompt 有**兩份且須同步**：addon `_sentence_prompt` 與 core `_sentence_instructions`（addon 不能 import core，改一邊要改另一邊，檔內已標 KEEP-IN-SYNC）。association 已串進 ⌘A / ⌘S / CLI 造句，不要再讓它只餵圖片。
- **Batch Operations 面板（⌘F / 選單 Batch Operations…）= 統一批量面板**，四個堆疊式 section：上 `TranslateSection`（批次補整句翻譯，沿用 `SentenceCNWorker`）、上-中 `ClearFlaggedSection`（清空紅旗卡）、下-中 `LongSentencesSection`（清空過長例句）、下 `TestCardsSection`（產生/清除測試卡）。`BatchOperationsDialog` 用 `QFrame` 分隔線（`_hline()`）組裝，section 各自 `QWidget` 自帶 scan/state，未來加新批量功能就再加一個 section widget。section 要關面板就呼叫傳入的 `panel.accept()`。
- **ClearFlaggedSection = 手機標紅旗 → Mac 清空+拔旗（刻意不生成）**。手機端**做不到**清/改欄位（AnkiMobile 無外掛、卡片模板 JS 不能寫欄位、連 flag/mark 都不行，Anki 開發者明言）→ 手機只用 **Anki 內建紅旗**標記。清空：對每張 `update_note` 清 6 欄（`REFILL_CLEAR_FIELDS`，保留 `Front`+`Association`）+ `set_user_flag_for_cards(0, cids)` 拔旗 → `save`+`reset`。**同步、瞬間、無 worker/進度條/Stop**。重生交給 ⌘S（清完顯示「Open Complete Missing Cards」一鍵跳 ⌘S，或 Done）。掃卡比照 `BackfillDialog` 用 `_looks_english` 略過非英文/空 Front；只認 `flag:1`；**列出清單 + 按 Clear 就是閘門，不做二次確認 dialog**。
- **LongSentencesSection = 找過長例句清空重生（只清不生）**。門檻可調（預設 20，`_clamp_length_threshold`）；字數 `_sentence_word_count`（去 HTML、空句/佔位符回 0 → 缺句歸 ⌘S 管）；清 `REBUILD_CLEAR_FIELDS`（Sentence/Sentence_CN/Audio/Translation/Image_Prompt 五欄——**換句=全重建**，Translation 依句中用法翻、Image 依句意搜，換句都該重來；只留 Front/Association/Front_Audio。使用者實測後定案）；不碰旗標；清完跳 ⌘S 重生。與 ClearFlaggedSection 同模式：同步、無 worker、列清單即閘門。純函式測試在 `test_long_sentences.py`。
- **TestCardsSection = 手動測 dialog 用的測試卡工具（開發輔助）**。建只有 `Front`+`Association` 的裸卡（Front 用純字母假詞 `zztest…`，因 `_looks_english` 擋數字）→ 它們會出現在 Complete Missing Cards 讓你勾選測試（如 Remove Selected）。全部打 tag `whiteforge_test`，Clean 靠 tag 一鍵刪。同步、直接動 `mw.col`（`new_note`/`add_note`/`find_notes`/`remove_notes`），無 worker。**與 CLI `make_test_cards.py` 是同一工具的兩份實作**（addon 不能 import core → tag 與假詞清單各存一份，標 KEEP-IN-SYNC；同 tag 故兩邊建的可互相清）。純邏輯 `_clamp_test_count`（Count 欄解析/夾限）抽出來給 pytest 測（`test_test_cards.py`）。
- **為何 ClearFlaggedSection 不再「清空後重新生成」（舊 Refill 的坑，已廢）**：舊設計把「重置」和「重生成」綁成一個動作，清旗綁在 `card_done`（語意是「處理完」非「填好」），而各 generation helper 失敗都**靜默回 `""` 不 raise** → 部分成功的卡照樣 emit `card_done` 被清旗 → 半成品/空卡被當完成、Refill 下次掃不到。拆開後：清空只清空（不會失敗），生成一律走 ⌘S（掃欄位內容、不靠旗子，會自動認出被清空的卡）。`RefillWorker`／`RefillFlaggedDialog` 已刪除。
- **批次類視窗（⌘A/⌘S/⌘D/⌘F）一律非阻塞開啟，走 `_show_nonmodal()`**（`aqt.dialogs.open`），**不要用 `exec()`**（application-modal 會鎖整個 Anki）。**`exec()` 除了「擋住使用者」還隱性提供三件事，非阻塞化必須自己補回**（第一版只換 show() 沒補，code review 抓出 6 個洞）：
  1. **批次互斥** → module 級 `_batch_acquire/_batch_release/_batch_busy`。同時只准一個批次在跑：兩個 worker 會搶同一份速率額度，且 ⌘S 與 ⌘F 都寫 `Sentence_CN` → 對同一批卡同一欄位重複寫。三個同步 section（清旗/清長句/測試卡）與 ⌘D 刪卡走 `_blocked_by_batch()` 同一道閘門。**新增任何會動卡片的功能都要接上**
  2. **關窗即結束批次** → `_BatchDialogMixin.done()` 在跑批中只請 worker 停、**不關窗**（視窗一關，worker 的 signal 就打到已刪除的 Qt 物件，`_on_finished` 還會在背後 `mw.col.save()`）。worker 收尾時各 dialog 呼叫 `_end_batch()` 才真的關。**關窗一定要走 `_force_close()` 不能走 `done()`**——worker 用的是自訂 `finished` signal（在 `run()` 返回前就發出），那一刻 `isRunning()` 仍是 True，走 `done()` 會被自己的守門擋掉、視窗永遠關不掉
  3. **Anki 退出/切 profile 的收拾** → 用 **Anki 內建 `aqt.dialogs`**（`register_dialog`/`open`/`markClosed`）而不是自製 registry：`closeAll()` 只看得到它自己的登記表，自製的看不到 → worker 會對正在卸載的 collection 續寫。內建的還附贈單例、還原被縮小的視窗、`reopen()` 重掃 hook。契約：每個視窗要有 `silentlyClose` 或 `closeWithCallback`（mixin 已提供），且**所有關窗路徑都必須經過 `markClosed`**（`done()` 是唯一漏斗：Close 鈕 `accept()`、X、Esc 都會走到它）
  - `reopen()` 一定要實作重掃：dialog 只在 `__init__` 掃卡，單例被叫回前面若不重掃，「⌘F 清空紅旗卡 → Open Complete Missing Cards」會看到清空前的舊清單、一鍵重生等於沒作用
  - 非阻塞後「視窗開著時卡片被別處刪掉」變成可達路徑 → 對快取的 note id 一律用 `_live_note()`（`mw.col.get_note` 會拋 `NotFoundError`），不要直接 `get_note`
  - worker 掛在 section 上而不是 dialog 上的視窗（Batch Operations）要覆寫 `_active_worker()`／`_set_batch_status()`
  - Settings 是設定視窗，維持 modal `exec()`。純邏輯測試在 `test_nonmodal_dialogs.py`
- **pytest 全綠不代表 Qt 沒事** <!-- @assert:path check_qt_compat.py -->：`conftest.py` 的假 aqt 把每個 Qt 類別換成 `_Any`（`AddWordDialog.__mro__` 裡根本沒有 `QDialog`）→ 那批測試驗的是純邏輯，對「這一版 Qt 還能不能用」**零覆蓋**。Anki / Qt 升級後要跑 `uv run python check_qt_compat.py`：它載入 **Anki.app 內實際在用的那份** PyQt6 與 aqt（`Contents/Resources/app_packages` 加進 `sys.path`；**只能加這個**，`Resources/app` 底下另一個 `anki` 套件會遮蔽真正的），用真 Qt 建構四個對話框、掛真快捷鍵、走 `aqt.dialogs` 的單例與 `closeAll()`。offscreen、不需要 Anki 開著。覆蓋面只到「想得到的破法」為止——遇到新的升級踩雷就往裡面加一條，不加它就永遠停在當時的覆蓋面
- **測試用的假 aqt 統一放 `conftest.py`**（module 層 `install_fake_aqt()`，conftest 保證比測試模組先載入）。測試檔直接 `import addon` 就好，**不要各自抄一份 stub**——曾經 7 個檔逐字重複，addon 多 import 一個 Qt 類別就要改 7 次。
- **對話框 UI 文字一律英文**（最後訂版語言規則，求一致）；但**程式註解 / docstring / LLM prompt 範例 / 中文偵測 regex 保持中文**。改 addon 對話框新增字串用英文。
- core 的 LLM 文字呼叫一律走 `core/dispatcher.py`（容量感知分流 Groq+Gemini、斷路器、failover）；
  provider 在 `core/providers.py`。Gemini 沒有 rate-limit header → 本地 bucket（配額常數 `GEMINI_RPM`）。
  兩家見底時 `groq_generate_strict` 翻譯成 `RateLimitReached(soonest_reset)`。addon 走鏡像子模組
  `addon/_llm_dispatch.py`（KEEP-IN-SYNC 對照 core 兩檔；自足 stdlib-only → pytest 直接檔案載入測，
  改動要同步雙邊）。`backfill_words.py` 橫幅用 `engine_description()`。

## Git 規則

- 以**一段完整功能**為單位 commit，不要每改幾行小東西就 commit（例如只改幾行中文、調個字串，不需單獨 commit）。把相關的程式、**文件（README / CLAUDE.md）**、測試**併進同一個功能 commit**——README 不要單獨拆成一個 commit。
- 一個 commit = 一段有意義的功能 / 修復 / 重構（連同它的文件與測試）；不同功能仍分開 commit，不要把多個不相關功能塞進同一個。
- commit type 用 conventional 風格：`feat` / `fix` / `docs` / `style` / `refactor` / `perf` / `chore` 等。commit 標題與內文依全域規則用**英文**（Workspace 專案 = 練英文 commit；2026-08-25 起）
- commit 前先跑 Pre-push Checklist
- 不確定要不要 commit 時，問使用者

## Running

所有 script：`uv run python <script>.py`，需 Anki 開著並啟用 AnkiConnect。模板部署：`uv run python update_template.py`。

## Pre-push Checklist

1. README.md 是否同步更新（使用面資訊有變動？）
2. CLAUDE.md 是否同步更新（踩雷點 / 規則有變動？）
3. Addon 改動在 `addon/`（symlink 到 Anki），改完重啟 Anki 驗證
4. 同一事實沒有同時寫進 README 和 CLAUDE.md（無重複、無 drift）
