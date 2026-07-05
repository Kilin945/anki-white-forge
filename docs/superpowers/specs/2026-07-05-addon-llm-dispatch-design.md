# Addon LLM 分流鏡像（Phase 2）

日期：2026-07-05
狀態：設計已核可，待寫實作計畫
前篇：`2026-07-05-llm-resilience-layer-design.md`（Phase 1，core 版，已完成）

## 背景與動機

Phase 1 讓 core / CLI 的 LLM 呼叫走 Groq+Gemini 容量感知分流；**addon（⌘A / ⌘S / 批量面板）仍是單 Groq**（addon 不能 import core）。使用者最痛的撞限場景正是 ⌘S（補 7 張就可能卡），Phase 2 把同一套韌性層鏡像進 addon。

## 現況（addon 的 LLM 面，已收斂）

- **單一 HTTP 閘道 `_groq_chat(prompt, *, temperature, max_tokens, timeout, strict=False)`**（urllib、`User-Agent: AnkiWordAdder/1.0`）。4 個消費者：`_groq_spellcheck`（timeout 8）、`_llm_sentence` 造句（12–15）、`_groq_translate`（10）、`_groq_translate_sentence`（15）。
- `_groq_limiter`（`_GroqLimiter`，讀 header）→ `BackfillWorker._process_one` 開頭 `wall_secs()>0` 就整批停。
- strict=True 撞 429 拋 `_AddonRateLimited(retry_after)` → `SentenceCNWorker` pacing 接。

## 決策記錄

| 議題 | 決定 |
|---|---|
| 程式碼位置 | **A：新子模組 `addon/_llm_dispatch.py`**（addon 是 package，symlink 蓋整個資料夾）。自足、只用 stdlib、零 aqt 依賴 → pytest 直接檔案載入測，免假 stub；不再讓 1768 行的 `__init__.py` 膨脹 |
| HTTP client | urllib（addon 慣例，非 requests/SDK）；Groq 必帶 `User-Agent: AnkiWordAdder/1.0` |
| 與 core 的關係 | KEEP-IN-SYNC 鏡像（檔頭標注，對照 `core/providers.py`+`core/dispatcher.py`）；語意一致，介面差異僅 `generate(...)` 多收 `timeout` |
| 呼叫端改動 | `_groq_chat` 簽名不變、4 個消費者不動；strict 把 `AllProvidersLimited` 翻譯成既有 `_AddonRateLimited(soonest_reset)` → SentenceCNWorker 零改動 |
| ⌘S 停批閘 | `_groq_limiter.wall_secs()` → dispatcher 的 `wall_secs()`：**兩家都不可用才 >0**，值 = 最快恢復秒數 |
| 撞限訊息 | 帶兩家真實 reset：`Both providers out of quota — Groq resets in ~40s, Gemini in ~15s. Completed X, Y still need filling.`（英文 UI）；單 Groq 退化時沿用現有措辭 |

## 架構

### `addon/_llm_dispatch.py`（新，自足）

鏡像 core 韌性層的緊湊版，內容：

- `_parse_int` / `_parse_reset_secs`（自 `__init__.py` 搬入）
- `HeaderLimiter`（原 `_GroqLimiter` 升級：加 `headroom()`（0–1 比例）、`mark_exhausted()`，保留 thread-safe）
- `LocalBucketLimiter`（Gemini 固定窗口本地桶，`GEMINI_RPM = 15` 常數）
- `CircuitBreaker`（三態 + **單筆試探閘 `_probe_inflight`**，與 core 修正版同步）
- `ProviderError` / `ProviderRateLimited(retry_after)`
- `GroqProvider`：urllib POST 至 `api.groq.com`，`llama-3.3-70b-versatile`，回應 header 餵 limiter；429 → `mark_exhausted` + `ProviderRateLimited`；**非預期例外包成 `ProviderError`**
- `GeminiProvider`：urllib POST 至 `generativelanguage.googleapis.com`（`gemini-2.0-flash`，key 走 `x-goog-api-key` header）；429 解析 `retryDelay`；200 但壞 JSON → `ProviderError`（Phase 1 F2 的教訓）
- key 檔：`~/Workspace/anki/.groq_key` / `.gemini_key`（與現有 `GROQ_KEY_PATH` 同式）；無 key 的 provider 不參戰
- `Dispatcher`：headroom 快照一次 → 降冪 → `allows()` 在攻擊前才問 → failover；非 `ProviderError` 例外也 `record_failure` 後 re-raise（釋放試探閘）；全不可用 → `AllProvidersLimited(resets, soonest_reset)`
- `Dispatcher.wall_secs() -> float`：任一 provider（breaker 放行且 headroom>0）可用 → 0.0；否則 min(各家 reset)。給 ⌘S 停批閘用
- `Dispatcher.resets() -> dict`：給撞限訊息組字用
- 差異：provider `generate(prompt, *, temperature, max_tokens, timeout)` 多 `timeout`，Dispatcher 原樣傳遞

**自足性要求**：無相對匯入、無 aqt —— `importlib.util.spec_from_file_location` 可單獨載入。

### `addon/__init__.py` 接線

- 頂部 `from . import _llm_dispatch as _lld`；模組層 `_dispatcher = _lld.Dispatcher([...load()...])`
- `_groq_chat` 內裡改為：無 provider → `""`；呼叫 `_dispatcher.generate(prompt, temperature=…, max_tokens=…, timeout=…)`；非 strict 吞一切回 `""`；strict 接 `AllProvidersLimited` → `raise _AddonRateLimited(int(soonest)+1)`
- `BackfillWorker._process_one` 的 `_groq_limiter.wall_secs()` → `_dispatcher.wall_secs()`；`BackfillWorker` 記 `resets` 供對話框組訊息
- `BackfillDialog._on_finished` 撞限分支訊息升級（見決策表）；只有 Groq 時退化為現有單家措辭
- **移除**：`_GroqLimiter`、`_parse_int`、`_parse_reset_secs`、`_groq_limiter`、`_groq_chat` 的 HTTP 內裡、`_parse_retry_after`（搬入子模組）。`_AddonRateLimited` 保留在 `__init__.py`（它是呼叫端協定）

### 消費者盤點（Phase 1 `_groq_client` 事故的教訓）

被移除符號的已知消費者：
- `test_groq_limiter.py`：測 `addon._GroqLimiter`/`_parse_*` → **更新為直接檔案載入 `addon/_llm_dispatch.py` 測 `HeaderLimiter`**（甩掉假 aqt stub），壓力測試保留
- `test_backfill_remove.py` / `test_test_cards.py`：用 stub import addon、只碰 `_drop_notes`/`TEST_CARD_WORDS` 等 → 不受影響，但**必須驗證仍綠**（stub 的 `_Any` 要能撐過 `from . import _llm_dispatch`——子模組是真檔，import 會真的執行，stdlib-only 所以可以）
- 實作時 grep 全 repo 確認移除符號零殘留

## 測試

1. **`test_addon_llm_dispatch.py`**（新）：`spec_from_file_location` 直接載入子模組（無 stub）。鏡像 core 測試面：兩個 limiter、breaker 三態+單筆試探、路由消長換邊、failover、`AllProvidersLimited` 帶 resets、`wall_secs()`（一家可用→0；全擋→最快 reset）、timeout 傳遞（fake provider 收到）、無 key 退化、Gemini 壞 JSON → `ProviderError`。假時鐘、免網路。
2. **`test_groq_limiter.py`** 改造：對子模組的 `HeaderLimiter` 跑原有 parser/牆偵測/並發壓力測試。
3. 既有 addon 測試（`test_backfill_remove.py`、`test_test_cards.py`）保持綠。

## 驗證（手動，需重啟 Anki）

1. 重啟 Anki → ⌘A 加一字 → 正常生成（走分流，無感）
2. Batch Operations → Test Cards 產 7 卡 → ⌘S 全選補完 → 正常
3. 拿掉 `.gemini_key` 重啟 → 一切照舊（單 Groq 退化）
4. 撞限訊息靠單元測試守（手動難觸發）

## 範圍外

- A 案（找過長舊句刪掉重建）——下一輪
- addon UI 顯示引擎名稱（YAGNI）
- core 與 addon 共用程式碼的機制（addon 不能 import core 是硬約束，維持 KEEP-IN-SYNC 雙份）

## 成功標準

1. 全部測試綠（新 + 既有）
2. 重啟 Anki 手動流程（上節 1–3）全過
3. `__init__.py` 淨變瘦（HTTP/limiter 內裡移出），`_llm_dispatch.py` 與 core 版語意一致
