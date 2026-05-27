# My Word Adder — 設定

`shortcuts` 設定三個功能的快捷鍵（Anki 格式；Mac 上 `Ctrl` 會對應到 `⌘`）：

- `add` — Add English Word（預設 `Ctrl+D` = ⌘D）
- `complete` — Complete Missing Cards（預設 `Ctrl+S` = ⌘S）
- `find_duplicates` — Find Duplicate Words（預設 `Ctrl+F` = ⌘F）

設成空字串 `""` 代表不綁快捷鍵，只能從 **Tools 選單**開啟。

例：把找重複改成 ⌘⇧F、其餘維持預設：

```json
{
  "shortcuts": {
    "add": "Ctrl+D",
    "complete": "Ctrl+S",
    "find_duplicates": "Ctrl+Shift+F"
  }
}
```

⚠️ **改完設定後要重啟 Anki 才會生效**（快捷鍵在 addon 載入時綁定）。
