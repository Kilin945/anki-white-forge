# 2026-08-31 — 模型退役救火 + 批次視窗非阻塞化

## 做了什麼

### 事故排查：三個欄位全都生不出來
- 症狀：⌘S 面板上 Sentence / Meaning / Translation 全橘，Audio / Image 正常，而且量很小（兩張卡）
- 根因一：兩家供應商把寫死的模型**同日退役**，API 回 HTTP 404。Groq `llama-3.3-70b-versatile`、Gemini `gemini-2.0-flash` 都不在清單上了
  - 換成 `openai/gpt-oss-120b` + `gemini-flash-latest`（浮動別名）
- 根因二：新模型都是**思考型**，思考 token 也算進 `max_tokens`。單字翻譯只給 32、拼字給 12 → 預算被思考吃光，回應是 `finish_reason=length` + 空正文
  - 加 `REASONING_HEADROOM=512`（provider 層加，呼叫端語意不變）+ 最低思考等級

### 兩個新功能
- **句子失敗擋下游**：句子不可用（空/佔位符）→ Image / Translation / Sentence_CN / 句音全跳過，只做 Front_Audio。純函式 `_sentence_usable` / `sentence_usable`，閘門接在 ⌘A / ⌘S / CLI 三處
- **造句用較高思考等級**：`effort` 參數貫穿 dispatcher + 兩家 provider，造句傳 `medium`，查詢式短呼叫維持 `low`

### 批次視窗非阻塞化（外加一輪 code review 修補）
- 四個視窗（⌘A/⌘S/⌘D/⌘F）從 `exec()` 換成非阻塞開啟
- code review 抓出 6 個洞，全部修掉：批次互斥、關窗先停批、`aqt.dialogs` 接管、`reopen()` 重掃、縮小還原、`_live_note` 防呆
- 測試 stub 從 7 份重複收斂到 `conftest.py` 一份

## 決定了什麼

- **Groq 用 120b 不用小模型**：實測三個候選模型在免費層拿到的速率配額**完全一樣**（1000 req / 8000 token per min），延遲只差 0.3 秒（MoE 架構）。所以「大模型比較耗」在這裡不成立，選品質最好的
- **Gemini 用浮動別名 `gemini-flash-latest`**：這次事故就是釘死版號造成的。Groq 沒有這種別名，退役風險仍在
- **造句 `medium`、其他 `low`**：造句是唯一多條件約束的任務（字義優先序、長度、不准鋪陳）。翻譯一個單字不需要深思，思考從 413 降到約 60 token，省 TPM
- **句子失敗就擋掉全部依賴句意的下游**，不做「部分照跑」：事故現場抓到 pedestrian 的 `Sentence_CN` 是「請為 pedestrian 新增」這個佔位符的中文翻譯。寧可整張卡下次一起重做，也不要欄位彼此不一致的髒卡
- **非阻塞化不是只換 `show()`**：`exec()` 隱性提供三件事——批次互斥、關窗即結束批次、Anki 退出時的收拾。第一版只換了「擋住使用者」，另外三件沒補，被 review 抓出來
- **用 Anki 內建 `aqt.dialogs` 而不是自製 registry**：一次拿到單例、縮小還原、`reopen()` hook、退出收拾。自製的 `closeAll()` 看不到 → worker 會對正在卸載的 collection 續寫
- **批次互斥範圍包含 ⌘A**：⌘A 雖然是單字、不寫別人的卡，但照樣搶速率額度。擋下並說明原因，比讓它撞限失敗好

## 學到 / 發現

- **模型退役會偽裝成撞限**：404 被當一般失敗走 failover，兩家都 404 就變成「所有供應商不可用」，log 看起來像速率問題。實際是模型不存在
- **思考型模型的 token 陷阱**：思考 token 算在輸出預算內，小預算呼叫會靜默回空字串。以後接新模型要先確認這件事
- **worker 的自訂 `finished` signal 在 `run()` 返回前就發出** → 那一刻 `isRunning()` 仍是 True。所以關窗不能走 `done()`（會被自己的守門擋掉、永遠關不掉），要走 `_force_close()`
- **`TranslateSection` 的第一個參數是 Qt parent，另外三個 section 是 panel**：簽名不一致，我照著寫 `self._panel` 就會 AttributeError。已統一
- **PyQt6 的 enum 不能直接 `int()`**（`ShortcutContext`），要用 `.name` / `.value`
- **可以在 Anki 內部自動化測 Qt 行為**：寫一個臨時 addon 掛 `profile_did_open`，用真的 dialog 物件、真的事件迴圈跑驗證，結果寫 JSON。比用 AppleScript 模擬鍵盤可靠得多；需要驗鍵盤事件時再配 System Events 送單一按鍵
- **快捷鍵穿透（review 的 PLAUSIBLE 發現）證明不成立**：四個 QAction 的 `shortcutContext` 都是 `WindowShortcut`，實測 ⌘A 視窗聚焦時送 ⌘S 不會觸發主視窗動作

## 下次繼續

- Gemini 這幾天常回 503「high demand」，長一點的造句請求特別容易中。目前靠 failover 給 Groq 接手，功能不受影響；如果變成常態，考慮把 Gemini 降為純備援
- Groq 免費層 8000 TPM 偏低，大量 backfill 仍靠既有 pacing。思考等級已壓到最低，再省就得縮 prompt
