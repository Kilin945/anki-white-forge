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

## 檔案說明

### 主程式

| 檔案 | 說明 |
|------|------|
| `add_word.py` | CLI 新增單字。輸入單字和聯想後，自動用 Groq 生成例句、Pexels 下載圖片、edge-tts 生成雙語音（Andrew 正面 + Ava 背面）。含拼字檢查，圖片和音檔平行生成。用法：`uv run python add_word.py <word> [association]` |
| `backfill_words.py` | 批次補齊所有缺少欄位的卡片。4 路跨卡片並發 + 卡片內圖片/音檔平行。句子和圖片查詢合併為一次 LLM 呼叫。句子重新生成時音檔也會跟著更新。用法：`uv run python backfill_words.py` |
| `regen_audio.py` | 重新生成所有卡片的音檔（句子 Ava + 單字 Andrew）。用於切換語音或修正錯誤音檔。用法：`uv run python regen_audio.py` |
| `update_template.py` | 更新 Anki 卡片的 HTML 模板和 CSS 樣式。包含完整的 Light Mode 科學配色和手機 RWD。用法：`uv run python update_template.py` |

### 輔助模組（Addon 透過 subprocess 呼叫）

| 檔案 | 說明 |
|------|------|
| `_image_helper.py` | 圖片搜尋模組。用 Groq 生成搜尋關鍵字，Pexels 下載（DuckDuckGo fallback）。Addon 以 subprocess 呼叫，因為 Anki 內建 Python 沒有 groq 套件。 |
| `_gtts_helper.py` | TTS 語音模組。用 edge-tts 生成 MP3。支援單檔模式和 `--batch` 模式（一次 subprocess 生成多個音檔，asyncio.gather 並行）。 |
| `_validate_helper.py` | 拼字檢查模組。Addon 呼叫用於驗證單字和聯想的拼寫。 |
| `debug_audio.py` | 音檔除錯工具。查看某個單字的句子文字和音檔狀態，生成測試音檔到 /tmp。用法：`uv run python debug_audio.py <word>` |

### Anki Addon

| 檔案 | 說明 |
|------|------|
| `~/Library/.../my_word_adder/__init__.py` | Anki 插件主程式。提供兩個 UI 入口：`Ctrl+Shift+W` 新增單字、`Ctrl+Shift+C` 補齊缺失卡片。LLM 用 urllib 直呼 Groq API（需 User-Agent），TTS/圖片透過 subprocess 呼叫上面的輔助模組。BackfillWorker 3 路並發，卡片內圖片/音檔平行。 |

### 設定與測試

| 檔案 | 說明 |
|------|------|
| `.groq_key` | Groq API 金鑰（gitignored） |
| `.pexels_key` | Pexels API 金鑰（gitignored） |
| `test_backfill.py` | 單元測試（41 tests）：純函數、LLM fallback、音檔生成、process_note 邏輯 |
| `test_integration.py` | 整合測試：新增 3 張卡片，驗證所有欄位填滿，自動清理 |
| `pyproject.toml` | Python 依賴定義（requests, groq, edge-tts, duckduckgo-search, pyspellchecker） |

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
