# Rebuild Long Sentences（過長例句清除重建）

日期：2026-07-05
狀態：設計已核可，待寫實作計畫

## 背景與動機

造句 prompt 的長度規則（「6-12 字、單一子句」）是後來才加的；之前生成的舊卡留下大量長句。實測牌組 810 句：平均 12.2 字、最長 27 字，**>20 字 55 張（7%）、>16 字 236 張（29%）、>12 字 397 張（49%）**。最長那批含字典定義句（`corruption`）、25+ 字場景長文（`transient`）——正是 prompt 現在禁止的型態。prompt 本身已夠好（使用者確認），問題只在**舊卡沒被重生**。

## 決策記錄

| 議題 | 決定 |
|---|---|
| 門檻 | **預設 >20 字**（55 張）先清一輪再看狀況；UI 做成可調（之後可降到 16 清第二波）|
| 機制 | **方案 A：Batch Operations 加 section、只清不生**。複製 `ClearFlaggedSection` 的成功模式；重生走現成管線（⌘S 互動分批 / CLI `backfill_words.py`+`backfill_sentence_cn.py` 大量節流）。**不重蹈舊 Refill「清+生綁一起」的坑** |
| 清除欄位 | `Sentence`、`Sentence_CN`、`Audio`（句子語音，隨句子連動）。**保留** `Translation`/`Image_Prompt`/`Front_Audio`（單字層級，不受換句影響）|
| 字數計算 | 去 HTML 後 `len(s.split())`（與探勘腳本一致）；空句與佔位符句**不算長句**（那是「缺句」，⌘S 的事）|
| 清單樣式 | 照 ClearFlagged 的字串列表，多標字數：`transient(27) · dig(26) · …`，依字數降冪 |
| 確認機制 | 與 ClearFlagged 一致：**列出清單 + 按 Clear 就是閘門，不做二次確認 dialog** |

## 架構

### 新 section：`LongSentencesSection`（`addon/__init__.py`）

`BatchOperationsDialog` 第四個堆疊 section（Translate → ClearFlagged → **LongSentences** → TestCards；放 TestCards 前——功能性 section 在前、開發輔助殿後）。自帶 scan/state，形狀完全比照 `ClearFlaggedSection`：

- **UI**：標題「Rebuild Long Sentences」+ 說明文字（英文）+ 門檻輸入 `Longer than [20] words` + Rescan + 字數降冪清單（scroll）+ 右下 `Clear N Sentences` 鈕；清完顯示 ✓ 狀態與 post-row（`Open Complete Missing Cards` / `Done`，沿用 `panel.accept()` 跳轉）
- **掃描**（同步、主執行緒）：`_deck_note_ids()` 全掃；去 HTML 後算字數；`_looks_english` 略過非英文 Front；空句/佔位符略過；字數 > 門檻者入列。門檻輸入解析比照 `_clamp_test_count` 模式抽純函式
- **清除**（同步、瞬間、無 worker）：對每張 `mw.col.get_note` → 清 `Sentence`/`Sentence_CN`/`Audio` 三欄 → `update_note` → 全部完成後 `save`+`reset`。無旗標操作（與 ClearFlagged 不同：這裡不涉紅旗）
- **重生**：不做。清完的卡自動變成 ⌘S 掃得到的「缺句卡」；大量時使用者自行走 CLI

### 純函式（可測，humble object 慣例）

- `_sentence_word_count(html_value) -> int`：去 HTML、strip、`len(split())`；空/佔位符回 0（0 永遠不超標 → 自然排除）
- `_clamp_length_threshold(raw, default=20) -> int`：門檻輸入解析/夾限（下限 1、非數字/空白回 default；上限不設──使用者要填 999 掃不到東西是合理結果）
- `_long_sentence_label(word, count) -> str`：清單項目 `f"{word}({count})"`

`REBUILD_CLEAR_FIELDS = ["Sentence", "Sentence_CN", "Audio"]` 模組常數（對照既有 `REFILL_CLEAR_FIELDS` 命名）。

## 測試

`test_long_sentences.py`（fake-aqt stub 模式，同 `test_backfill_remove.py`）：

1. `_sentence_word_count`：普通句、含 HTML 標籤、空字串、佔位符（回 0）、前後空白
2. `_clamp_length_threshold`：正常數字、空白→20、非數字→20、0/負→1、大數照收
3. `_long_sentence_label`：格式正確
4. `REBUILD_CLEAR_FIELDS` 恰為三欄（防止手滑加欄位）

Qt/`mw.col` 層（scan/clear/跳轉）走手動驗證。

## 手動驗證（重啟 Anki）

1. ⌘F → 看到第四個 section「Rebuild Long Sentences」，門檻預設 20
2. Scan 出 ~55 張、字數降冪（最上面應是 transient(27)、dig(26)…）
3. `Clear 55 Sentences` → ✓ 訊息；Anki 裡抽查一張：Sentence/Sentence_CN/Audio 空、Translation/Image/Front_Audio 仍在
4. `Open Complete Missing Cards` → 這批卡出現在 ⌘S 清單 → 分批補完 → 新句子應在 6-12 字左右
5. 門檻改 16 → Rescan → 數量變多（~236-55 張級距）

## 範圍外

- 自動重生成（走現成 ⌘S / CLI 管線）
- CLI 版 find/clear（YAGNI，>16 那波若嫌 UI 麻煩再議）
- 重生後「驗長度不合格自動再清」的迴圈（先看一輪成果）

## 成功標準

1. 新測試綠、既有 188 測試不壞
2. 手動流程 1-5 全過
3. 清除後的卡在 ⌘S / CLI 重生後，句長回到 prompt 規範（抽查）
