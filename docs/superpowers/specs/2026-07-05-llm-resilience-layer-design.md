# LLM 韌性層（Groq + Gemini 分流）

日期：2026-07-05
狀態：設計已核可，待寫實作計畫

## 背景與動機

目前所有 LLM 文字呼叫（造句、單字翻譯、整句翻譯、拼字檢查）**只打 Groq**（`llama-3.3-70b-versatile`），沒有備案（地端 Ollama fallback 已因記憶體移除）。撞到速率上限時的行為：

- **⌘S（Complete Missing Cards）**：`_groq_limiter` 讀 header 主動偵測，接近上限就整批乾淨停下、回報「completed X, Y left, try again in ~N s」。已補的保留、沒補的原封不動。
- **批量（backfill_sentence_cn）**：撞 429 等 `Retry-After` 再續（pacing）。
- **單發呼叫（非 strict）**：失敗靜默回 `""`，欄位留空、下次再掃到。

痛點：Groq 免費額度小（實測 ⌘S 補 7 張就可能撞牆），撞牆後只能等。Gemini 免費層是**另一池獨立額度**，接進來可以近乎倍增吞吐，且一家出事另一家頂上。

## 決策記錄（brainstorming 定案）

| 議題 | 決定 |
|---|---|
| Gemini 角色 | **C：分流（active-active）**——兩家對等、無主從；不是 fallback 備援 |
| 分派策略 | **容量感知（capacity-aware）**：每筆呼叫前比較兩家 headroom（剩餘額度），挑大的 |
| 輸出風格一致性 | 不需要——prompt 已把句長/選義規則寫死，兩家照做即可，混用無妨 |
| 兩家同時見底 | **看場景分**：互動（⌘S）停下回報、批量等最近的 reset 續跑；政策放呼叫端，dispatcher 只 raise |
| 提示訊息 | 升級為帶真實 reset 時間：「Both providers out of quota — Groq resets in ~40s, Gemini in ~15s. Completed X, Y left.」 |
| 實作範圍 | **1+2+3+4**：Provider 抽象＋容量路由＋斷路器＋failover。Retry/backoff、健康度追蹤、MQ、bulkhead 先不做 |
| 分期 | **Phase 1 = core（本輪）**：完整實作＋pytest。**Phase 2 = addon 鏡像（另一輪）**：`_groq_chat` 換緊湊版 dispatcher、接 ⌘A/⌘S/批量面板、升級提示文字 |

## 架構（Phase 1）

```
呼叫端（llm_sentence / llm_translate / backfill scripts …）
        │  介面完全不變
        ▼
core/llm.py::groq_generate(_strict)      ← 內部改走 dispatcher
        ▼
core/dispatcher.py  Dispatcher           ← 單一決策點
   1. 過濾：斷路器 OPEN 或 headroom == 0 的 provider 排除
   2. 路由：剩下的挑 headroom 最大者
   3. 打它；成功 → 更新 limiter/breaker → 回傳
      失敗 → breaker 記失敗 → failover 打另一家
   4. 兩家都不可用 → raise AllProvidersLimited(soonest_reset, per_provider_resets)
        │                │
        ▼                ▼
core/providers.py  GroqProvider    GeminiProvider     ← 對等，各自帶 limiter
```

### 檔案佈局

| 檔案 | 動作 | 內容 |
|---|---|---|
| `core/providers.py` | 新增 | Provider 介面 + GroqProvider + GeminiProvider（各帶 headroom limiter）|
| `core/dispatcher.py` | 新增 | 容量感知路由 + 斷路器 + failover + `AllProvidersLimited` |
| `core/llm.py` | 修改 | `groq_generate` / `groq_generate_strict` 內部改走 dispatcher；所有 prompt 函式不動 |
| `core/rate_limiter.py` | 不動 | `BatchLimiter` 是批次記帳層，與 provider 韌性層各司其職 |

**切入點選擇**：改在 `groq_generate` 內部，所有既有呼叫端（含 backfill scripts）一行不改，爆炸半徑最小。（函式名之後可再改成 `llm_generate`，Phase 1 不強求。）

### Provider 介面

```python
name: str                    # "groq" / "gemini"
generate(prompt, *, temperature, max_tokens) -> str    # 失敗 raise，不吞
headroom() -> float          # 剩餘額度（0.0 = 沒額度）；額度未知（尚無資料）視為充足
reset_secs() -> float        # 幾秒後額度恢復（0 = 現在就可用）
```

- **GroqProvider**：現有 groq SDK 呼叫搬入；limiter **讀 rate-limit header**（`x-ratelimit-remaining-tokens` 等，做法移植 addon `_GroqLimiter`——core 目前沒有這層，補上）。
- **GeminiProvider**：模型 `gemini-2.0-flash`；key 檔 `.gemini_key`（比照 `.groq_key`，gitignore）。**Gemini API 不回 rate-limit header** → limiter 用**本地 token bucket**：自己數當前窗口的呼叫數，配額寫成可調常數（實作時查官方文件填當時數字）。**沒 key 時 GeminiProvider 不參戰**——headroom 恆 0，系統退化為單 Groq，行為與現狀相同，不會壞。

### 斷路器（每個 provider 一顆）

- 三態：CLOSED →（連續 N 次失敗，N=3）→ OPEN →（冷卻到期）→ HALF-OPEN →（試探成功→CLOSED / 失敗→OPEN）。
- **冷卻時間 = 該家的 `reset_secs()`**（算得出來就不瞎猜）；拿不到時預設 30s。
- 429 與其他錯誤（5xx、timeout）都算失敗；成功即清零連續失敗計數。

### 兩家見底時（政策在呼叫端）

- dispatcher raise `AllProvidersLimited`，帶每家的 reset 秒數與最快恢復時間。
- **`groq_generate_strict` 把它翻譯成既有的 `RateLimitReached(retry_after=soonest_reset)`** → 批量呼叫端（`backfill_sentence_cn.py`）的既有 catch／pacing 邏輯**一行不改**就自動變成「等兩家中最快恢復的那個」。
- **互動呼叫端**（⌘S，Phase 2 接）：接住 → 停下回報，訊息含兩家各自的恢復時間（per-provider 細節那時才需要，Phase 1 例外物件先把資料帶好）。
- 單發非 strict 呼叫（`groq_generate`）：維持現狀，接住任何失敗靜默回 `""`。

## 測試（Phase 1 主菜，全部免網路）

Provider 用假物件（可編程的 headroom / 成功 / 失敗序列）注入 dispatcher：

1. **路由**：挑 headroom 大者；headroom 消長會換邊；一家恢復後路由自動回來。
2. **斷路器**：三態轉換正確；OPEN 期間不被選；冷卻（用假時鐘 patch `time.monotonic`）到期後半開試探；試探成功復位／失敗再關。
3. **failover**：選中者單筆失敗 → 另一家接手且成功回傳；兩家都失敗 → `AllProvidersLimited` 帶正確 reset 秒數。
4. **Gemini 本地 bucket**：窗口內計數遞減 headroom；窗口過後 reset。
5. **退化**：無 `.gemini_key` → 只剩 Groq 可選，行為等同現狀。

風格沿用既有測試（`test_groq_limiter.py`）：pytest、假時鐘、class 分組。

## 範圍外（明確排除）

- 圖片（Pexels/圖搜）、TTS（gTTS）不是 LLM 呼叫，不經過這層。
- Retry/backoff、健康度追蹤、MQ／工作佇列、bulkhead —— 對單人 addon 過度工程，不做。
- addon 端接線（Phase 2 另開一輪：鏡像 dispatcher、KEEP-IN-SYNC、⌘S 提示文字升級）。
- A 案「找出過長舊句子刪掉重建」——等本層完成後另開，屆時批量重建可吃到雙倍額度。

## 前置作業（使用者）

至 Google AI Studio（aistudio.google.com）申請免費 Gemini API key，存到 `~/Workspace/anki/.gemini_key`。沒 key 前一切照舊可用。

## 成功標準

1. 全部新測試綠、既有 104 測試不壞。
2. 有 `.gemini_key` 時：`backfill_sentence_cn.py` 大批量跑，可觀察到兩家交替被選用、單家撞限不中斷整批。
3. 無 `.gemini_key` 時：行為與現狀完全相同。
