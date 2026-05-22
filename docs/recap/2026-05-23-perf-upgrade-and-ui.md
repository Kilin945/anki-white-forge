# 2026-05-23 — 效能大改造 + 科學配色 UI 重設計

## 做了什麼

### 效能優化
- backfill_words.py 加入 ThreadPoolExecutor，跨卡片 4 路並發 + 卡片內 image/audio 並行
- Ollama 替換為 Groq API（llama-3.3-70b-versatile），句子生成從 5-10s 降到 0.5s
- gTTS 替換為 edge-tts：正面 Andrew 男聲唸單字、背面 Ava 女聲唸句子
- 兩次 Ollama LLM 呼叫合併為一次（llm_sentence_and_query），句子+圖片查詢同時產出
- _gtts_helper.py 加入 --batch 模式，一次 subprocess 生成多個音檔（asyncio.gather）
- Addon BackfillWorker 改為 3 路並發（ThreadPoolExecutor），Worker 內 image/audio 並行
- 新增 Front_Audio 欄位 + AnkiConnect 自動加欄位 + 更新模板

### Bug 修復
- Addon urllib 呼叫 Groq API 缺 User-Agent header → 403 → 靜默 fallback Ollama（根本原因）
- _fetch_image 的 `--` 參數放錯位置，導致 argparse 吃掉 --definition/--sentence → 圖片永遠失敗
- BackfillWorker 用 Worker.__new__ 跳過 QThread.__init__，_ollama_sentence 的 self.progress.emit() 會炸
- backfill 重新生成句子時音檔沒跟著更新（has_audio=True 就跳過）
- Image_Prompt 手動刪除後殘留 `<div><br></div>`，_scan 判定非空 → 改用 `<img` 標籤檢測
- add_word.py 原本根本沒下載圖片，只放文字描述到 Image_Prompt

### UI 重設計
- 卡片配色從 Monokai（黑底綠字）→ 科學配色 Light Mode
- 背景 #FDFBF7 乳白、單字 #1E293B 石墨灰、定義 #0284C7 湛藍、句子 #64748B 知性灰
- 句子中的單字自動標橘色 #EA580C（Von Restorff 效應，JS 自動匹配）
- 字體換成 Poppins（標題）+ Inter（內文）
- 左圖右文佈局、手機響應式、圖片 object-fit: contain
- Addon 進度顯示：打勾 UI + 引擎標示（Groq/Ollama）

### 檔案同步
- 所有 .py 統一升級：backfill_words.py、add_word.py、regen_audio.py、_image_helper.py、_gtts_helper.py、debug_audio.py
- Addon __init__.py 同步升級 Groq + edge-tts + Front_Audio + 並發
- 新增 CLAUDE.md 記錄每個檔案職責
- 新增 test_backfill.py（41 個 pytest 測試）+ test_integration.py

## 決定了什麼

- **Groq 取代 Ollama 作為主要 LLM**：免費額度夠用（14,400 RPD），0.5s vs 5-10s。Ollama 保留作 fallback
- **edge-tts 取代 gTTS**：聲音品質大幅提升，雙語音（Andrew/Ava）。風險：逆向工程微軟端點，但個人低用量可接受
- **Addon 與 CLI 不共用模組**：兩邊跑在不同 Python 環境（Anki 內建 vs uv venv），addon 用 subprocess 呼叫 helper scripts，LLM 部分各自實作。維護成本換取零依賴衝突
- **圖片判斷用 `<img` 標籤檢測**：不能用 strip_html 因為會把 img 標籤本身也清掉
- **Light Mode 科學配色**：基於認知心理學研究 — 暖色引導注意（單字）、冷色邏輯理解（定義）、低飽和降低負荷（句子）

## 學到 / 發現

- urllib 預設不帶 User-Agent，Groq API 會直接 403 拒絕。groq SDK 用 httpx 自動帶 User-Agent 所以沒問題
- argparse 的 `--` 分隔符之後的所有東西都被當位置參數，optional flags 必須放在 `--` 之前
- Worker.__new__(Worker) 跳過 QThread.__init__，pyqtSignal 需要 QObject 初始化才能 emit
- Anki 編輯器刪除圖片時會殘留空 HTML 標籤（`<div><br></div>`），不是空字串
- edge-tts batch 模式用 asyncio.gather 可以同時生成多個音檔，省掉多次 subprocess 開銷
- Von Restorff 效應：在例句中用高對比色標出目標單字，大腦會自動聚焦

## 下次繼續

- 97 張卡片的 Complete Missing Cards 還沒跑完（重啟後應正常，只剩真正缺圖的）
- Anki 主題需切成 Light Mode 才能完整呈現乳白配色
- pyproject.toml 還留著 gtts 依賴（已不使用），duckduckgo-search 未宣告
- commit 所有改動
