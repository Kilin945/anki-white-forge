# My Daily English — Anki 自動化單字系統

個人英文單字學習系統，基於 Anki + AnkiConnect，輸入單字後自動生成例句、圖片、語音。

---

## 系統架構

| 組件 | 技術 | 用途 |
|------|------|------|
| LLM | **Groq API**（Ollama fallback） | 生成例句 + 圖片搜尋關鍵字 |
| TTS | **edge-tts** | 正面 Andrew 男聲唸單字、背面 Ava 女聲唸句子 |
| 圖片 | **Pexels API**（DuckDuckGo fallback） | 下載單字插圖 |
| Anki | AnkiConnect addon | 程式與 Anki 溝通 |

---

## 牌組結構

**牌組**：`My_Daily_English`
**筆記類型**：`English_White_Method`

| 欄位 | 說明 | 填寫方式 |
|------|------|----------|
| `Front` | 單字 | 手動輸入 |
| `Association` | 中文聯想（可選） | 手動輸入 |
| `Sentence` | 英文例句 | 自動（Groq LLM） |
| `Image_Prompt` | 插圖 | 自動（Pexels） |
| `Audio` | 句子語音 (Ava) | 自動（edge-tts） |
| `Front_Audio` | 單字發音 (Andrew) | 自動（edge-tts） |

---

## 前置需求

### Anki 套件
- **AnkiConnect**（代碼 `2055492159`）：Anki 必須開著才能運作

### API Keys
```bash
# Groq（免費，https://console.groq.com）
echo "gsk_your_key_here" > ~/Workspace/Anki/.groq_key

# Pexels（免費，https://www.pexels.com/api）
echo "your_key_here" > ~/Workspace/Anki/.pexels_key
```

### Python 環境
```bash
cd ~/Workspace/Anki
uv sync   # 自動安裝所有依賴
```

---

## 日常使用

### 方式一：Anki UI（推薦）

**新增單字**：`Ctrl+Shift+W` → 輸入單字 → Enter
- 自動生成例句、圖片、雙語音
- 有拼字檢查和重複防呆

**補齊缺失卡片**：`Ctrl+Shift+C`
- 掃描所有缺少欄位的卡片
- 3 張並發處理，左圖右文即時進度顯示

### 方式二：Terminal

```bash
cd ~/Workspace/Anki

# 新增單字
uv run python add_word.py "glimpse" "a brief look"

# 批次補齊所有空白欄位（4 路並發）
uv run python backfill_words.py

# 重新生成所有音檔（換語音後用）
uv run python regen_audio.py
```

---

## 手機新增 → Mac 補齊

1. 手機 AnkiMobile → 新增卡片（只填 Front + Association）→ 同步
2. Mac Anki → 同步
3. `Ctrl+Shift+C`（Complete Missing Cards）或 `uv run python backfill_words.py`
4. Mac Anki → 同步（選「上傳到 AnkiWeb」）
5. 手機 → 同步 → 完整卡片出現

---

## 卡片配色

基於認知心理學「3 層視覺階層」設計（Light Mode）：

| 層級 | 元素 | 顏色 | 原理 |
|------|------|------|------|
| 引導注意 | 句中單字 | `#EA580C` 焦糖橘 | Von Restorff 孤立效應 |
| 邏輯理解 | 中文定義 | `#0284C7` 湛藍 | 冷色促進概念連結 |
| 降低負荷 | 例句 | `#64748B` 知性灰 | 低飽和減少疲勞 |
| 背景 | 底色 | `#FDFBF7` 乳白 | WCAG 對比度 5:1 |

字體：**Poppins**（標題）+ **Inter**（內文）
佈局：左圖右文、手機響應式（圖上文下，圖片限高 200px）

---

## 專案結構

```
Anki/
├── core/                    # 共用模組
│   ├── anki.py              # AnkiConnect API
│   ├── image.py             # Pexels + DuckDuckGo 圖片
│   ├── llm.py               # Groq + Ollama LLM
│   ├── text.py              # strip_html, normalize, is_placeholder
│   └── tts.py               # edge-tts (Andrew + Ava)
├── templates/               # Anki 卡片模板
│   ├── front.html
│   ├── back.html
│   └── style.css
├── add_word.py              # CLI 新增單字
├── backfill_words.py        # 批次補齊欄位
├── regen_audio.py           # 重生所有音檔
├── update_template.py       # 套用模板到 Anki
├── debug_audio.py           # 音檔除錯
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
| `core/llm.py` | LLM 統一入口。Groq API 優先，Ollama fallback。句子生成、圖片查詢、合併呼叫都在這裡 |
| `core/tts.py` | TTS 語音生成。edge-tts wrapper，定義 Andrew（正面）和 Ava（背面）語音 |
| `core/image.py` | 圖片搜尋下載。Pexels API 優先，DuckDuckGo fallback |
| `core/text.py` | 文字處理。strip_html、normalize、is_placeholder、has_image |
| `core/anki.py` | AnkiConnect API wrapper |

### 模板 `templates/`

| 檔案 | 說明 |
|------|------|
| `templates/style.css` | 卡片 CSS。科學配色 Light Mode + 手機 RWD |
| `templates/front.html` | 正面 HTML（單字 + 播放鍵） |
| `templates/back.html` | 背面 HTML（左圖右文 + 單字高亮 + 播放鍵 JS 定位） |

### 主程式

| 檔案 | 說明 |
|------|------|
| `add_word.py` | CLI 新增單字。用法：`uv run python add_word.py <word> [association]` |
| `backfill_words.py` | 批次補齊缺少欄位。4 路並發。用法：`uv run python backfill_words.py` |
| `regen_audio.py` | 重新生成所有音檔。用法：`uv run python regen_audio.py` |
| `update_template.py` | 讀取 `templates/` 並更新 Anki 模板。用法：`uv run python update_template.py` |
| `debug_audio.py` | 音檔除錯。用法：`uv run python debug_audio.py <word>` |

### Addon subprocess 模組

| 檔案 | 說明 |
|------|------|
| `_image_helper.py` | 圖片搜尋 CLI。Addon 以 subprocess 呼叫 |
| `_gtts_helper.py` | TTS CLI。支援 `--batch` 模式 |
| `_validate_helper.py` | 拼字檢查 CLI |

### Anki Addon

| 檔案 | 說明 |
|------|------|
| `~/Library/.../my_word_adder/__init__.py` | Anki 插件主程式。`⌘D` 新增單字、`⌘S` 補齊缺失卡片。LLM 用 urllib 直呼 Groq API，TTS/圖片透過 subprocess。BackfillWorker 3 路並發 |

### 設定與測試

| 檔案 | 說明 |
|------|------|
| `.groq_key` | Groq API 金鑰（gitignored） |
| `.pexels_key` | Pexels API 金鑰（gitignored） |
| `test_backfill.py` | 單元測試（38 tests） |
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
A：確認 Groq 有在用（進度顯示 `Groq`），如果顯示 `Ollama` 代表 Groq 失敗了，檢查 `.groq_key`

**Q：同步時出現衝突對話框？**
A：選「上傳到 AnkiWeb」— 電腦端是最新的
