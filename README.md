# My Daily English — Anki 自動化設定說明

個人英文單字學習系統，基於 Anki + AnkiConnect，輸入單字後自動抓取例句、圖片、語音。

---

## 牌組結構

**牌組名稱**：`My_Daily_English`  
**筆記類型**：`English_White_Method`

| 欄位 | 說明 | 填寫方式 |
|------|------|----------|
| `Front` | 單字（查詢 key） | 手動輸入 |
| `Association` | 關聯字（可選） | 手動輸入 |
| `Sentence` | 例句 | 自動（Free Dictionary API）|
| `Image_Prompt` | 圖片 | 自動（DuckDuckGo 圖片搜尋）|
| `Audio` | 語音（唸例句） | 自動（Google TTS）|

---

## 前置需求

### Anki 套件
- **AnkiConnect**（代碼 `2055492159`）：讓腳本與 Anki 溝通的橋梁，Anki 必須開著才能運作

### Python 環境
```bash
cd ~/Workspace/Anki
uv venv .venv
uv pip install requests gtts ddgs
```

---

## 日常使用

### 方式一：Anki UI 視窗（推薦）

Anki 上方選單 → **工具 > Add English Word…**（或 `Ctrl+Shift+W`）

1. 輸入單字
2. 輸入 Association（可選）
3. Enter 或點 Add Card
4. 等待自動補齊完成（約 5–15 秒）

有重複防呆，同一個單字不會重複加入。

### 方式二：Terminal（批次或快速用）

```bash
cd ~/Workspace/Anki

# 只輸入單字
.venv/bin/python add_word.py "serendipity"

# 加上關聯字
.venv/bin/python add_word.py "ephemeral" "fleeting, transient"
```

---

## 手機新增單字後在 Mac 補齊欄位

在手機 AnkiMobile 新增卡片時，只填 **Front** 和 **Association**，其餘留空。

回到 Mac 後：

```
1. 手機 Anki → 同步
2. Mac Anki → 同步
3. 執行：
   cd ~/Workspace/Anki && .venv/bin/python backfill_words.py
4. Mac Anki → 再次同步
5. 手機 Anki → 同步 → 完整卡片出現
```

或直接使用 Claude Code 的 `/anki` skill 自動完成步驟 3。

---

## 檔案說明

```
~/Workspace/Anki/
│
├── add_word.py          # CLI 新增單字腳本
├── backfill_words.py    # 批次補齊空白欄位（手機新增後用）
├── update_template.py   # 更新卡片模板 CSS / HTML 版面
├── _gtts_helper.py      # TTS helper（Anki add-on 呼叫用）
├── _image_helper.py     # 圖片下載 helper（DuckDuckGo）
├── pyproject.toml       # Python 套件依賴定義
└── .venv/               # Python 虛擬環境

~/Library/Application Support/Anki2/addons21/my_word_adder/
├── __init__.py          # Anki add-on 主程式（UI 視窗）
└── manifest.json        # add-on 設定

~/.claude/skills/anki/
└── SKILL.md             # /anki Claude skill（補齊 + 狀態確認）
```

---

## 卡片版面（CSS）

- **桌機**：左側文字（Association + Sentence + Audio），右側圖片佔 55%
- **手機**：圖片在上，文字在下（`order: -1` + `flex-direction: column`）
- 圖片使用 `object-fit: contain`，不裁切
- 字體大小使用 `clamp()` 自適應視窗寬度

修改版面後執行：
```bash
cd ~/Workspace/Anki && .venv/bin/python update_template.py
```

---

## 常見問題

**Q：加完單字在 Anki 沒看到？**  
A：確認 Anki 有開著（AnkiConnect 需要 Anki 在背景運行）

**Q：手機沒有圖片或聲音？**  
A：media 檔案需要同步，桌機同步後等底部出現 `Syncing media…` 完成，再讓手機同步

**Q：例句是定義句不是真實例句？**  
A：Free Dictionary API 對某些字沒有例句，會用定義代替，可手動在 Anki 瀏覽器修改

**Q：某個單字圖片不對？**  
A：在 Anki 瀏覽器手動替換 `Image_Prompt` 欄位的內容即可
