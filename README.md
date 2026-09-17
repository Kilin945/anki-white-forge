# My Daily English — Anki 自動化單字系統

個人英文單字學習系統，基於 Anki + AnkiConnect。輸入一個單字，例句、圖片、語音全部自動生成。

---

## 系統架構

| 組件 | 技術 | 用途 |
|------|------|------|
| LLM | **Groq API + Gemini API**（容量感知分流，見下） | 生成例句 + 圖片搜尋關鍵字 |
| TTS | **edge-tts** | 正面 Andrew 男聲唸單字、背面 Ava 女聲唸句子 |
| 圖片 | **Pexels API**（DuckDuckGo fallback） | 下載單字插圖 |
| Anki | AnkiConnect addon | 程式與 Anki 溝通 |

> LLM 呼叫（`core/llm.py`）會依剩餘額度在 Groq 和 Gemini 之間自動分流，一家額度見底就切到另一家。沒設定 `.gemini_key` 就只用 Groq，功能不變。CLI、core 和 Anki Addon（⌘A、⌘S、批量面板）走的都是同一套分流。

---

## 牌組結構

**牌組**：`My Daily English`
**筆記類型**：`English_White_Method`

| 欄位 | 說明 | 填寫方式 |
|------|------|----------|
| `Front` | 單字 | 手動輸入 |
| `Association` | 中文聯想（可選） | 手動輸入 |
| `Sentence` | 英文例句 | 自動（Groq LLM） |
| `Image_Prompt` | 插圖 | 自動（Pexels） |
| `Audio` | 句子語音 (Ava) | 自動（edge-tts） |
| `Front_Audio` | 單字發音 (Andrew) | 自動（edge-tts） |
| `Translation` | 單字中文翻譯（背面點擊顯示） | 自動（Groq LLM） |
| `Sentence_CN` | 整句中文翻譯（背面點擊顯示） | ⌘A 即時、⌘S 補齊、或 Batch Operations 面板批次（Groq LLM） |

> 背面的 `Translation`（單字）與 `Sentence_CN`（整句）都是點一下才顯示的填空框。

---

## 前置需求

### Anki 套件
- **AnkiConnect**（代碼 `2055492159`）：Anki 必須開著才能運作

### API Keys
```bash
# Groq（免費，https://console.groq.com）
echo "gsk_your_key_here" > ~/Workspace/anki/.groq_key

# Gemini（可選，免費，https://aistudio.google.com/apikey）
# 有設定就雙 provider 容量感知分流；沒設定就只用 Groq，功能照常
echo "your_key_here" > ~/Workspace/anki/.gemini_key

# Pexels（免費，https://www.pexels.com/api）
echo "your_key_here" > ~/Workspace/anki/.pexels_key
```

> Key 檔在 Anki 插件啟動時讀一次。新增或更換 key 後要重啟 Anki 才生效。CLI 腳本則是每次執行時讀。

### Python 環境
```bash
cd ~/Workspace/anki
uv sync   # 自動安裝所有依賴
```

---

## 日常使用

### 方式一：Anki UI（推薦）

四個功能視窗（`⌘A` / `⌘S` / `⌘D` / `⌘F`）都不會鎖住 Anki。生成跑很久的時候，可以把視窗移開或縮小，回到 Anki 繼續背卡。

同一個視窗只會有一個。視窗已經開著時再按同一個快捷鍵，是把它叫回最前面，不是開第二個。

同一時間只跑一個批次。前一個批次還在跑時按別的生成按鈕，會被擋下並告訴你是哪個視窗在跑——兩個批次同時跑會互搶 API 速率額度，也可能對同一批卡重複寫入。

批次跑到一半按 Close，會先停批：做完手上那張卡，視窗才關閉。

#### 新增單字：`⌘A`

按 `⌘A`，輸入單字，按 Enter。例句、圖片、雙語音、單字翻譯、整句翻譯全部自動生成。

輸入有三道防呆。非英文字元直接擋下。Groq 會檢查拼字，疑似拼錯時建議正確的字。重複的字也會擋，比對前先正規化，所以大小寫或 HTML 變體騙不過它。

#### 補齊缺失卡片：`⌘S`

掃描所有缺欄位的卡片，缺什麼補什麼：例句、整句翻譯、圖、音、單字翻譯。3 張並發處理，左圖右文顯示即時進度。

手機或 Anki 內建介面新增的卡片沒經過 ⌘A，欄位是空的，⌘S 一鍵補完，整句翻譯也包含在內。要大量回填的話，改走下面的 Batch Operations 面板，以免撞到速率上限。

開窗時所有卡片預設是勾起來的，按一下 Complete Selected 就整批開跑。想跳過某幾張（拼錯的字、之後再處理的），自己取消它們的勾選；也可以用最上面的 Select all 一次全部取消，再挑要補的那幾張。

#### 批量操作：`⌘F` 或 Tools → Batch Operations…

一個面板，由上而下四塊，之後有新批量功能就往下加。

**Backfill Sentence Translations（批次補整句翻譯）**
專門補 `Sentence_CN`。開啟時先顯示共幾筆、預估幾分鐘。選一個時間盒（1、2、5、10 分鐘）或直接跑完，翻譯節奏控制在 Groq 速率內（約每分鐘 25 句）。隨時可以按 Stop，下次打開從沒翻的地方繼續。

**Clear Flagged Cards（清空紅旗卡）**
手機複習時看到不理想的卡（例句不貼切、翻譯有誤），先用 Anki 內建的紅旗標起來。回到 Mac 開這個面板，它會列出所有紅旗英文卡。按 Clear N Cards 之後，卡片只留 Word 和 Association，其餘六欄（例句、兩個翻譯、圖、字音、句音）清空，旗子也拔掉。清空是瞬間完成的，不會重新生成。想馬上補，按 Open Complete Missing Cards 一鍵跳去 ⌘S；想之後再補就按 Done。只認紅旗（flag:1），非英文卡略過。

為什麼繞這一圈：手機的卡片模板寫不了欄位，紅旗是手機上唯一能做的記號。

**Rebuild Long Sentences（重建過長例句）**
舊卡的例句生成得比「6-12 字」規則進 prompt 更早，常常過長。填一個字數門檻（預設 20 字），按 Rescan 列出超標的卡，顯示「單字(字數)」、字數多的排前面。按 Clear N Sentences 會清空例句、整句翻譯、句子語音、單字翻譯和圖，只保留單字和單字發音。換了句子就等於全部重建，翻譯和圖都該跟著重來。清空同樣瞬間完成、不重新生成。清完一鍵跳 ⌘S 重生短句；量大時改走 CLI。

**Test Cards（測試卡）**
開發、測試輔助。填 Count（預設 7）按 Add Test Cards，產生只有 Front + Association 的裸卡。這些卡會出現在 Complete Missing Cards，可以拿來測補卡流程。Clean Test Cards 一鍵刪光。它和 CLI 的 `make_test_cards.py`（`add [N]`、`clean`）用同一個 tag，兩邊建的可以互相清。

#### 找重複單字：`⌘D`

把正規化後 Front 相同的卡片分組列出，抓得到手機漏進來的 HTML 或大小寫變體。勾選要刪的卡（每組至少保留一張），確認後刪除。

> ⌘A、⌘S、⌘D、⌘F 都可以在 **Tools → My Word Adder Settings…** 直接按組合鍵重新設定，即時生效，不用改 JSON；清除設定就等於關閉該快捷鍵。
>
> 對話框的 UI 文字一律英文（統一語言）。

### 方式二：Terminal

```bash
cd ~/Workspace/anki

# 新增單字
uv run python add_word.py "glimpse" "a brief look"

# 批次補齊所有空白欄位（例句、圖、音、單字翻譯，4 路並發）
uv run python backfill_words.py

# 批次回填整句翻譯 Sentence_CN（撞速率上限自動等 60s 續跑，Ctrl-C 結束）
uv run python backfill_sentence_cn.py

# 重新生成所有音檔（換語音後用）
uv run python regen_audio.py
```

---

## 手機新增 → Mac 補齊

1. 手機 AnkiMobile 新增卡片，只填 Front + Association，然後同步
2. Mac Anki 同步
3. 按 `⌘S`（Complete Missing Cards）一鍵補完所有欄位，含整句翻譯。走 CLI 的話注意：`backfill_words.py` 不補整句翻譯，要另跑 `backfill_sentence_cn.py`
4. Mac Anki 同步，選「上傳到 AnkiWeb」
5. 手機同步，完整卡片出現

---

## 卡片配色

基於認知心理學「3 層視覺階層」設計（Light Mode）：

| 層級 | 元素 | 顏色 | 原理 |
|------|------|------|------|
| 引導注意 | 句中單字 | `#EA580C` 焦糖橘 | Von Restorff 孤立效應 |
| 邏輯理解 | 中文定義 | `#0284C7` 湛藍 | 冷色促進概念連結 |
| 降低負荷 | 例句 | `#64748B` 知性灰 | 低飽和減少疲勞 |
| 背景 | 底色 | `#FDFBF7` 乳白 | WCAG 對比度 5:1 |
| 互動 | 翻譯填空框外框 | `#d6cfc4` 米色 | 透明底＋細外框＝可點但不搶戲 |

字體：**Poppins**（標題）+ **Inter**（內文）。

佈局是左圖右文，手機響應式改為圖上文下，圖片限高 200px。單字翻譯與整句翻譯是點擊顯示的填空框，兩個同款同字級。

---

## 專案結構

```
Anki/
├── core/                    # 共用模組
│   ├── anki.py              # AnkiConnect API
│   ├── image.py             # Pexels + DuckDuckGo 圖片
│   ├── llm.py               # Groq LLM
│   ├── rate_limiter.py      # 通用 429 偵測 / 批次節流
│   ├── text.py              # strip_html, normalize, is_placeholder
│   └── tts.py               # edge-tts (Andrew + Ava)
├── templates/               # Anki 卡片模板
│   ├── front.html
│   ├── back.html
│   └── style.css
├── addon/                   # Anki 插件原始碼（symlink → Anki addons 資料夾）
│   ├── __init__.py
│   └── manifest.json
├── add_word.py              # CLI 新增單字
├── backfill_words.py        # 批次補齊欄位（不含整句翻譯）
├── backfill_sentence_cn.py  # 批次回填整句翻譯 Sentence_CN
├── regen_audio.py           # 重生所有音檔
├── update_template.py       # 套用模板到 Anki
├── debug_audio.py           # 音檔除錯
├── normalize_fronts.py      # 正規化既有 Front（去 HTML / 轉小寫）
├── _image_helper.py         # Addon subprocess: 圖片
├── _gtts_helper.py          # Addon subprocess: TTS
├── _validate_helper.py      # Addon subprocess: 拼字
├── test_backfill.py         # 單元測試
├── test_integration.py      # 整合測試
├── .groq_key                # API key (gitignored)
├── .pexels_key              # API key (gitignored)
└── pyproject.toml
```

---

## 檔案說明

### 共用模組 `core/`

| 檔案 | 說明 |
|------|------|
| `core/llm.py` | LLM 統一入口。句子生成、圖片查詢、合併呼叫都在這裡 |
| `core/tts.py` | TTS 語音生成。edge-tts wrapper，定義 Andrew（正面）和 Ava（背面）語音 |
| `core/image.py` | 圖片搜尋下載。Pexels API 優先，DuckDuckGo fallback |
| `core/text.py` | 文字處理。strip_html、normalize、is_placeholder、has_image |
| `core/anki.py` | AnkiConnect API wrapper |

### 模板 `templates/`

| 檔案 | 說明 |
|------|------|
| `templates/style.css` | 卡片 CSS。科學配色 Light Mode + 手機 RWD |
| `templates/front.html` | 正面 HTML（單字 + 播放鍵） |
| `templates/back.html` | 背面 HTML。左圖右文、句中單字高亮、播放鍵用 JS 定位。兩個點擊顯示的翻譯框用 `<button>` 做，因為 AnkiMobile 的原生手勢會略過非互動元件 |

### 主程式

| 檔案 | 說明 |
|------|------|
| `add_word.py` | CLI 新增單字。用法：`uv run python add_word.py <word> [association]` |
| `backfill_words.py` | 批次補齊缺少欄位（例句、圖、音、單字翻譯，不含整句翻譯）。4 路並發。用法：`uv run python backfill_words.py` |
| `backfill_sentence_cn.py` | 批次回填整句翻譯 `Sentence_CN`。撞速率上限自動等待續跑，可 Ctrl-C 結束，下次續跑。用法：`uv run python backfill_sentence_cn.py` |
| `regen_audio.py` | 重新生成所有音檔。用法：`uv run python regen_audio.py` |
| `update_template.py` | 讀取 `templates/` 並更新 Anki 模板。用法：`uv run python update_template.py` |
| `debug_audio.py` | 音檔除錯。用法：`uv run python debug_audio.py <word>` |
| `normalize_fronts.py` | 正規化既有 Front：去殘留 HTML、轉小寫，全大寫縮寫（如 ASAP）保留。預設只預覽，加 `--apply` 才寫入 |

### Addon subprocess 模組

| 檔案 | 說明 |
|------|------|
| `_image_helper.py` | 圖片搜尋 CLI。Addon 以 subprocess 呼叫 |
| `_gtts_helper.py` | TTS CLI。支援 `--batch` 模式 |
| `_validate_helper.py` | 拼字檢查 CLI |

### Anki Addon

| 檔案 | 說明 |
|------|------|
| `addon/__init__.py` | Anki 插件主程式，symlink 到 `~/Library/.../addons21/my_word_adder/`。⌘A、⌘S、⌘D、⌘F 四個功能的實作都在這裡，用法見上面「日常使用」。LLM 走 `addon/_llm_dispatch.py` 以 urllib 直呼（Groq + Gemini 分流鏡像），TTS 和圖片透過 subprocess，補卡 BackfillWorker 3 路並發。改完需重啟 Anki |

### 設定與測試

| 檔案 | 說明 |
|------|------|
| `.groq_key` | Groq API 金鑰（gitignored） |
| `.pexels_key` | Pexels API 金鑰（gitignored） |
| `test_backfill.py` | 單元測試（57 tests） |
| `test_integration.py` | 整合測試（新增 3 字驗證） |
| `pyproject.toml` | Python 依賴定義 |

---

## 常見問題

**Q：加完單字在 Anki 沒看到？**
A：確認 Anki 有開著（AnkiConnect 需要 Anki 在背景運行）

**Q：手機沒有圖片或聲音？**
A：media 檔案需要同步，桌機同步後等 `Syncing media…` 完成，再讓手機同步

**Q：某個單字圖片不對？**
A：在 Anki 瀏覽器刪除 `Image_Prompt` 欄位內容，再跑 `Ctrl+Shift+C` 重新搜圖

**Q：音檔唸的是 placeholder 文字？**
A：跑 `uv run python regen_audio.py` 重新生成所有音檔

**Q：Complete Missing Cards 跑太慢？**
A：確認 Groq 有在用（進度顯示 `Groq`）。造句失敗時不退地端，會留 placeholder 等下次補。檢查 `.groq_key` 與網路

**Q：同步時出現衝突對話框？**
A：選「上傳到 AnkiWeb」— 電腦端是最新的

**Q：想看 Complete Missing Cards / LLM 呼叫的除錯紀錄？**
A：看 `logs/addon_llm.log`（自動產生、輪替保留最近 3MB）
