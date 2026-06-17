# Refill Flagged Cards — 設計文件

日期：2026-06-18
狀態：設計定案，待寫實作計畫

## 目的

在手機（AnkiMobile）複習時，看到內容爛掉的卡（例如舊 prompt 生成的差例句），
標一個**紅旗**。回 Mac 後跑一個動作：把紅旗卡「**保留單字與提示、其餘欄位清空重生**」，
讓既有的補卡引擎重新產出乾淨內容。

## 背景與關鍵限制

- 卡片模板是**唯讀渲染**，模板裡的 JavaScript 改不到筆記欄位。
- AnkiMobile **不支援外掛**，且開發者明言「用 JavaScript 在 AnkiMobile 標記/旗標卡片 is not possible」。
- 因此**手機卡背放按鈕清欄位是做不到的**。可行的手機端標記只能用 Anki **內建** UI：
  - 紅旗：在複習畫面的工具列（齒輪）→ Flag 1，或指派到 tap/swipe/top bar。
- 結論：手機只負責「標紅旗」（內建、零模板改動）；清空＋重補全部在 Mac 端做。

## 範圍

- **牌組／筆記類型**：`My_Daily_English` / `English_White_Method`（沿用 `_deck_note_ids` 同條件）。
- **觸發旗標**：只認**紅旗**（`flag:1`）。紅旗 = 「要重補」專用訊號，其他顏色旗標不受影響。
- **保留欄位**：`Front`（單字）、`Association`（提示）。
- **清空並重生欄位**：`Sentence`、`Sentence_CN`、`Image_Prompt`、`Audio`、`Front_Audio`、`Translation`。
  - 對應使用者語彙：例句、整句翻譯、字義翻譯、圖片、句音、字音。

## 為什麼要「先清空」

既有 `BackfillWorker` 只補**空的**欄位、且**句子已存在就不重生**（只補缺漏）。
要強迫整張重做，必須先把 6 個欄位清空，才能讓同一引擎全部重生。
清空正是這個功能的本質（「砍掉重練」），不是副作用。

## 架構

### 手機端
零改動。使用者用 Anki 內建紅旗標記要重做的卡。

### Mac 端（addon）
沿用既有的選單 / 快捷鍵 / 設定機制，新增一個入口與一個對話框。
重補引擎**完全複用** `BackfillWorker`，不新增任何生成邏輯。

**選單與快捷鍵**
- Tools 選單新增 `Refill Flagged Cards…`，與現有三項並列。
- 預設快捷鍵 **⌘G**（`Ctrl+G`），透過既有 `DEFAULT_SHORTCUTS` + `SettingsDialog` 可改 / 可清空。
- 實作點：
  - `DEFAULT_SHORTCUTS` 加 `"refill_flagged": "Ctrl+G"`。
  - 新增 `open_refill_flagged_dialog()` handler。
  - `_add_menu_action("Refill Flagged Cards…", "refill_flagged", open_refill_flagged_dialog)`。
  - `SettingsDialog.LABELS` 加 `("refill_flagged", "Refill Flagged Cards")`。

**對話框（版面照抄 ⌘B Backfill 對話框結構：狀態文字 + 進度條 + 鈕；UI 文字一律英文）**

```
┌──────────────  Refill Flagged Cards  ──────────────┐
│                                                      │
│  Word and Association are kept. All other fields     │
│  (sentence, both translations, image, word audio,    │
│  sentence audio) are cleared and regenerated.        │
│                                                      │
│  ┌──────────────────────────────────────────────┐  │
│  │ chunk · crux · decision · defer · flagged ·    │  │ ← scrollable word list
│  │ follow-up · granted · reimplement · stray      │  │
│  └──────────────────────────────────────────────┘  │
│                                                      │
│  ▓▓▓▓▓▓░░░░░░  Refilling: defer (5/9)                │ ← live current word
│                                                      │
│        ┌───────────┐          ┌───────────┐         │
│        │   Stop     │          │   Start    │         │
│        └───────────┘          └───────────┘         │
└──────────────────────────────────────────────────────┘
```

- **打開時**：掃 `flag:1` 的卡 → 列出單字（可捲動）。數量為動態，開視窗當下有幾張算幾張。
- **沒紅旗卡時**：清單區顯示 `No flagged cards.`，`Start` 鈕變灰。
- **頂部固定說明句**：`Word and Association are kept. All other fields (sentence, both translations, image, word audio, sentence audio) are cleared and regenerated.`
- **執行中狀態列**：`Refilling: <word> (i/N)`；完成顯示 `Refilled N card(s).`
- **鈕**：`Start`（執行）、`Stop`（中斷）。關視窗走右上紅燈 / Esc。
  - 未執行：Start 可按、Stop 灰。
  - 執行中：Start 灰、Stop 可按。
- 語言規則（最後訂版）：**全 app 畫面文字一律英文**，求一致。本對話框即依此全英文。
  現有其他對話框（⌘D / ⌘S / ⌘F / ⌘B / Settings）目前中英混雜，將於本功能完成後另開任務統一轉英文。

### 資料流（按「開始」後）
1. 取紅旗卡 note id：`mw.col.find_cards('deck:"My_Daily_English" note:"English_White_Method" flag:1')` → 映射到 note。
2. 逐張清空 6 欄（保留 `Front`、`Association`）並存檔。
3. 把這些 note（欄位現已全空）組成 `BackfillWorker` 吃的 note dict 格式，交給 `BackfillWorker` 重補。
4. 每張 `card_done` → 清掉該卡紅旗（`set_user_flag_for_cards(0, [card_id])`），進度條前進、更新當前單字。
5. 全部完成 → 狀態列回報「重補 N 張完成」。

### 停止行為
按「停止」中斷：已補完的卡保留新內容且已清旗標；未補到的卡維持紅旗、內容維持（若已被清空則仍空）。
下次再開「Refill Flagged Cards…」會重新掃到還有紅旗的卡，接著做。

## 安全性

- 破壞性清空前需明確按「開始」，不會點選單就執行。
- 紅旗本身即為使用者在 browser/手機的明確選取；對話框再列出單字供二次確認。
- 只動紅旗卡、只在指定牌組；保留 Word + Association 確保重補時例句語意（sense priority）不跑掉。

## 複用清單

- `_deck_note_ids` 的牌組/筆記類型條件
- `BackfillWorker`（重補引擎，零修改）
- ⌘B Backfill 對話框的版面 / 樣式（Qt widget，非卡片 HTML，不受「CSS 完全分開」限制）
- 既有選單 / 快捷鍵 / `SettingsDialog` 機制

## 不做（YAGNI）

- 不支援其他顏色旗標（只紅旗）。
- 不做 CLI 版本（手動標旗的工作流就在 GUI；大量場景另有 backfill 系列）。
- 不在卡片模板加任何按鈕（平台做不到）。
- 不刪舊媒體檔（清欄位後孤兒媒體由 Anki 的「檢查媒體」處理）。
