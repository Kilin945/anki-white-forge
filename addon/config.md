# My Word Adder — 設定

**最簡單的方式：** Anki 選單 **Tools → My Word Adder Settings…**，每個功能的欄位點一下、直接按你要的組合鍵即可（按「清除」＝不綁）。**免改 JSON、按儲存即時生效、不用重啟。**

---

下面的 JSON 是給進階使用者直接編輯用的。`shortcuts` 是各功能的快捷鍵（Anki 格式；Mac 上 `Ctrl` 對應 `⌘`）：

- `add` — Add English Word（預設 `Ctrl+A` = ⌘A）
- `complete` — Complete Missing Cards（預設 `Ctrl+S` = ⌘S）
- `find_duplicates` — Find Duplicate Words（預設 `Ctrl+D` = ⌘D）
- `backfill_cn` — Batch Operations 面板（批次翻譯 + 清空紅旗）（預設 `Ctrl+F` = ⌘F）

空字串 `""` = 不綁快捷鍵，只能從 Tools 選單開。

⚠️ 直接改這個 JSON 要**重啟 Anki** 才生效；用上面的 Settings 畫面則即時生效。
