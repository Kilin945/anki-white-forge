# 例句整句中文翻譯（Sentence_CN）設計

日期：2026-06-01
牌組：`My_Daily_English`／筆記類型：`English_White_Method`

## 目標

在卡片背面的英文例句（`Sentence`）下方，新增一塊顯示**整句繁體中文翻譯**的區域。
- 預設藏字、點一下才顯示（與單字翻譯 `cn-hint` 的操作一致）。
- 用稍深的米色背景框與整片大背景做視覺區別。
- 新單字（⌘D）自動產生此翻譯；既有大量舊卡透過批次回填補上。

## 非目標

- 不改 `Translation`（單字層級翻譯）的既有行為。
- 不重做例句生成或圖片邏輯。
- 不為此功能引入任何雲端服務或新相依套件（沿用既有 Groq）。

---

## 1. 資料模型：新欄位 `Sentence_CN`

在筆記類型 `English_White_Method` 新增欄位 **`Sentence_CN`**，順序放在 `Sentence` 之後。
- 內容：例句的繁體中文翻譯（純文字）。
- 空字串代表「尚未翻譯」，是完整性判斷與批次回填的依據。

新增欄位為一次性手動動作（Anki → 管理筆記類型 → 欄位），或由部署腳本檢查。實作時於部署步驟明列。

---

## 2. 翻譯生成：獨立函式

不與例句生成合併，維持與既有 `llm_translate(word, sentence)` 一致的獨立函式模式，方便批次「句子已存在只缺翻譯」時單獨補。

### core 端 `core/llm.py`

新增 `llm_translate_sentence(sentence)`：
- prompt：把給定英文句子翻成自然、完整的繁體中文，**只輸出譯文**，不要解釋、不要引號。
- 沿用既有 `llm()`（Groq SDK，失敗 fallback Ollama）。
- 防呆：回傳若仍含過多英文字母（疑似前言／拒絕），視為失敗回 `""`，留待下次重補。

### addon 端 `addon/__init__.py`

新增 `Worker._groq_translate_sentence(sentence)`：
- 沿用共用 `_groq_chat` helper（urllib 直呼 Groq，帶 `User-Agent: AnkiWordAdder/1.0`）。
- 同樣的防呆檢查（拒絕含英文字母的回應 → 回 `""`）。

兩邊 prompt 文字相同，邏輯各自實作（addon 不能 import core）。

---

## 3. 三處接線

| 路徑 | 檔案 | 行為 |
|------|------|------|
| 單張新增（⌘D） | `add_word.py` / `addon/__init__.py` ⌘D 流程 | 例句確定後翻譯整句，連同其他欄位一起寫入 `Sentence_CN`。呼叫量小，**不套用速率控制**。 |
| 大量回填（⌘S Complete） | `addon/__init__.py` BackfillWorker | 完整性判斷加入「缺 `Sentence_CN`」；沿用既有逐卡進度框 UI；**套用速率控制**。 |
| 大量回填（CLI） | `backfill_words.py` + 新 `backfill_sentence_cn.py` | `process_note` 加 `need_sentence_cn`；句子變動（`need_sentence`）時強制重翻。 |

### 完整性判斷更新

三處的「卡片是否完整」條件都加上 `bool(Sentence_CN)`：
- `backfill_words.py`：`process_note` 的 skip 條件、`main()` 的 `pending` 過濾。
- `addon/__init__.py` ⌘S：`incomplete` 判斷與逐卡欄位狀態 signal。

**影響**：所有既有舊卡因缺 `Sentence_CN` 會被視為不完整，下次 ⌘S 或批次回填會逐一補上（一次性大量處理，符合預期）。單張新增不受影響。

### 句子變動時重翻

`backfill_words.py` 既有 `need_sentence` flag：當句子被重新生成時，`Sentence_CN` 也必須重翻（舊翻譯對不上新句）。在 `need_sentence or not has_sentence_cn` 時都翻譯。

---

## 4. 速率控制（可重用模組）

### 設計動機

批次回填會連續打數百次 Groq；`llama-3.3-70b-versatile` 在 Groq 免費層 RPM 上限約 30。目前 `_groq_chat` / `groq_generate` 把 429 吞掉回 `""`，無法分辨「速率限制」與「翻譯失敗」。批次回填需要能分辨並主動煞車。

### 模組：`core/rate_limiter.py`（通用、可重用）

- 設定：每批上限 `batch_limit`（預設 25）。
- 提供狀態：已處理數、是否達上限、是否偵測到 429。
- 偵測 Groq 回 429（SDK 的 `RateLimitError` 或 urllib 的 HTTP 429）時，標記煞車、停止當批。
- 不含 Anki／欄位邏輯，純粹的批次節流／煞車器，未來其他批次功能可 import 重用。

### 批次行為（resume，非定時硬跑）

- 不存進度檔。**resume 天然達成**：每次只挑「仍缺 `Sentence_CN`」的卡片，所以隨時 Ctrl-C 中斷、下次再跑都從未處理的繼續。
- 跑滿一批（達 `batch_limit` 或偵測到 429）即停下，回報「已處理 X 張、剩餘 Y 張、請稍後再跑」。
- fallback：任一張翻譯失敗（含 429）不中止整批的資料完整性 —— 失敗的卡片保持 `Sentence_CN` 空，自然會在下次被重挑。

### `batch_limit` 校準（dry-run）

`25` 為保守預設。實作階段以安全的 dry-run 校準確認實際值：
- 連續呼叫翻譯 API（**不寫卡片**）直到第一次 429，記錄成功數與耗時 → 得出實際每分鐘安全批量。
- 會消耗少量 API 額度，但不更動任何卡片。
- 校準結果回填成 `batch_limit` 預設。

### CLI 進度顯示做區別

`backfill_sentence_cn.py` 終端輸出與其他 script 視覺區隔，例如：

```
┌─ Sentence_CN 回填 ──────────────────┐
│  ●●●●●●●●●●●●●●●●●●●●●●●●●  25/25     │
│  ⚠ 已達本批上限（速率保護），請 60 秒後再跑
│  剩餘 312 張未處理（下次自動從這裡續）
└─────────────────────────────────────┘
```

addon ⌘S 路徑沿用既有逐卡進度框，達上限時於框內顯示「遞 60 秒」提示。

---

## 5. UI：稍深米色翻譯框（響應式）

### `templates/back.html`

在 `.sentence` 與 `.play-row` 之間插入：

```html
<div class="sentence-cn" onclick="this.classList.toggle('revealed')">{{Sentence_CN}}</div>
```

### `templates/style.css`

```css
.sentence-cn {
  margin-top: 4px;
  padding: 14px 18px;
  background: #F0EBE3;          /* 稍深米色，與 #FDFBF7 大背景區別（與圖片側同色階）*/
  border: 1px solid #E2DDD5;
  border-radius: 10px;
  font-size: clamp(20px, 3vw, 26px);
  line-height: 1.7;
  color: transparent;          /* 預設藏字，只見深色空框 */
  cursor: pointer;
  user-select: none;
  transition: color 0.2s;
}
.sentence-cn.revealed { color: #475569; }

@media (max-width: 600px) {    /* 手機縮一階 */
  .sentence-cn {
    padding: 10px 14px;
    font-size: clamp(16px, 4vw, 19px);
    text-align: left;
  }
}
```

- 色彩取自既有色階（`#FDFBF7` 背景 → `#F0EBE3` 框 → `#E2DDD5` 邊框 → `#475569` 字），保持米色調一致。
- 預設 `color: transparent` 藏字、框框常駐；點擊 toggle `.revealed` 顯示。
- 空 `Sentence_CN`（尚未回填）時框內無字，與「已藏字」視覺一致，不突兀。

---

## 6. 測試

- `core/llm.py`：`llm_translate_sentence` 對正常句回中文、對失敗／英文前言回 `""`（既有測試風格）。
- `core/rate_limiter.py`：模擬達上限與 429 → 正確標記煞車、停止當批；未達上限正常累計。
- `backfill_words.py` / `test_backfill.py`：completeness 含 `Sentence_CN`；句子變動連動重翻；缺翻譯被列入 pending。
- 手動：`update_template.py` 部署後，桌機與手機各驗證 —— 框顯示、點擊顯示／隱藏、米色區別、RWD 縮放。

---

## 7. 部署步驟

1. 筆記類型 `English_White_Method` 新增欄位 `Sentence_CN`（放 `Sentence` 之後）。
2. `uv run python update_template.py` 部署 `back.html` + `style.css`。
3. addon 改動在 `addon/`（symlink 到 Anki）→ **重啟 Anki** 驗證。
4. dry-run 校準 `batch_limit`。
5. 跑批次回填補既有舊卡（可分多批、隨時中斷續跑）。
6. Pre-push Checklist：README / CLAUDE.md 同步、無重複事實。

---

## 開放項目（實作前確認）

- `batch_limit` 實測值（dry-run 後定）。
- addon ⌘S 達速率上限時的提示文案最終定稿。
