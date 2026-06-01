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

`Sentence_CN` 只由兩條路徑填，刻意與「補全部欄位」分開，避免大量未節流翻譯：

| 路徑 | 檔案 | 行為 |
|------|------|------|
| 單張新增（⌘D） | `add_word.py` / `addon/__init__.py` ⌘D 流程 | 例句確定後翻譯整句，連同其他欄位一起寫入 `Sentence_CN`。呼叫量小，**不套用速率控制**，顯示 `Sentence-CN` 進度 box。 |
| 大量回填（專用） | addon 選單「批次回填整句翻譯」(`SentenceCNDialog`) + CLI `backfill_sentence_cn.py` | 只處理 `Sentence_CN`；**burst 引擎 + 時間盒選單**（見 §4）。 |

**刻意不碰 `Sentence_CN` 的路徑**：

- **⌘S Complete**（`addon/__init__.py` BackfillWorker）：維持原樣，completeness 不含 `Sentence_CN`，進度 box 用 `BACKFILL_BOXES`（不含 Sentence-CN）。
- **`backfill_words.py`**（CLI 補全部欄位）：`note_complete()` 不檢查 `Sentence_CN`。

理由：加入後所有舊卡都會被判為不完整，會在「補全部」路徑觸發未節流的大量翻譯（撞速率上限）。改由專用 burst 工具負責，職責分明。`note_complete()` helper 仍抽出共用（DRY），只是不含 `Sentence_CN`。

---

## 4. 速率控制：持續節流（pacing）+ 時間盒選單

### 設計動機與模型

`llama-3.3-70b-versatile` 免費層 RPM ~25-30（令牌桶：用完後每幾秒回補一個）。**長期吞吐量被 RPM 卡死**（N 筆至少 `⌈N / RPM⌉` 分鐘）。

**模型 = 持續節流（pacing）**：以不超過速率的節奏**持續**翻譯；撞 429 就等 `Retry-After`（≈令牌回補的幾秒）再續同一筆，**直到選定的秒數用完**（`直接完成` = 直到全部翻完）。

> 為何不用 burst（爆發→等滿 60s→再爆發）：burst 對**短於一個視窗**的預算失效 —— 例「30 秒」只能做 1 輪，且剛跑過時那輪會秒撞 429、2 秒就結束，沒「用滿 30 秒」。pacing 則在 30 秒內隨令牌回補陸續翻 ~12 筆，真正用滿時間且吞吐最大。

### 共用偵測：`core/rate_limiter.py` + addon `_AddonRateLimited`

- `RateLimitReached(retry_after=60)` / `_AddonRateLimited(retry_after=60)`：429 例外，攜帶秒數（讀 `Retry-After`，缺失退 60）。
- `is_rate_limit_error(exc)`、`groq_generate_strict` / `_groq_chat_strict`：surface 429 而非吞掉。
- 純偵測邏輯，無 Anki／欄位知識，未來其他資料的批次也能重用。

### addon 專用選單 `SentenceCNDialog`（主路徑）

「批次回填整句翻譯」選單（Tools，無快捷鍵）：
- 開啟先掃描缺 `Sentence_CN` 的卡，顯示**預估時間** `共 N 筆，約 24/分 → 全部約 ⌈N/24⌉ 分鐘` + pacing 說明。
- 5 顆時間盒按鈕，標籤含預估筆數（`secs/60 × RPM`，capped）：`30 秒(~12) / 2 分鐘(~48) / 5 分鐘(~120) / 10 分鐘(~240) / 直接完成(全部)`。
- `SentenceCNWorker`（QThread）跑 pacing 引擎：`_groq_translate_sentence(strict=True)` 翻譯、AnkiConnect 寫回；撞 429 等 `Retry-After` 重試同筆。狀態顯示「翻譯中 X/N（剩 Ns）」「額度回補中 Ns」。
- 若 `Retry-After` 過長（> ~130s，可能每日額度）→ 停下回報，不在程式裡乾等。
- **resume 天然達成**：每次開啟重新只挑缺的；隨時「停止」，下次續。

### CLI `backfill_sentence_cn.py`

run-to-completion 版：跑到 429 → 等 `max(60, Retry-After)` 秒 → 續，直到全部完成；按 **Ctrl-C** 結束（下次再跑從缺的續）。

```
┌─ Sentence_CN 回填 ──────────────────┐
│  ●●●●●●●●●●●●●●●●●●●●●  本輪完成 21 張
│  剩餘 312 張未處理
└─────────────────────────────────────┘
達 Groq 速率上限， 60s 後自動續跑…（Ctrl-C 結束）
```

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
