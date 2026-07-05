# Rebuild Long Sentences（過長例句清除重建）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Batch Operations 面板新增第四個 section「Rebuild Long Sentences」：找出超過門檻字數的例句、一鍵清除句子相關三欄，讓現成的 ⌘S / CLI 管線重生短句。

**Architecture:** 完全複製 `ClearFlaggedSection` 的成功模式（同步掃描/清除、無 worker、列清單即閘門、清完跳 ⌘S）。決策邏輯抽三個純函式 + 一個常數（humble object），Qt 薄殼手動驗。**只清不生**——重生走既有管線。

**Tech Stack:** PyQt（Anki 內建）、pytest（fake-aqt stub 模式，同 `test_backfill_remove.py`）。

**Spec:** `docs/superpowers/specs/2026-07-05-rebuild-long-sentences-design.md`

## Global Constraints

- **只清不生**：清除欄位恰為 `REBUILD_CLEAR_FIELDS = ["Sentence", "Sentence_CN", "Audio"]`；保留 `Translation`/`Image_Prompt`/`Front_Audio`/`Front`/`Association`。不碰旗標。
- 門檻預設 **20**、UI 可調；字數 = 去 HTML 後 `len(s.split())`；空句與佔位符句（`PLACEHOLDERS` 命中）**回 0**（缺句是 ⌘S 的事，不入列）。
- 掃描比照既有 section：`_looks_english` 略過非英文/空 Front；清單依字數**降冪**、樣式 `word(count)` 以 ` · ` 相接。
- **列出清單 + 按 Clear 就是閘門，不做二次確認 dialog**（與 ClearFlagged 一致）。
- 對話框 UI 字串英文；註解/docstring 中文。
- 既有 188 測試不壞；addon 改動經整資料夾 symlink 自動部署，**重啟 Anki 驗證**。
- 一個 commit（feat，含測試與 README/CLAUDE.md 文件）。

---

### Task 1: 純函式 + `LongSentencesSection` + 面板組裝 + 文件

**Files:**
- Modify: `addon/__init__.py`（三處：純函式加在 `_looks_english` 附近；`LongSentencesSection` 加在 `TestCardsSection` 定義之前；`BatchOperationsDialog.__init__` 組裝插入一行）
- Test: `test_long_sentences.py`（新）
- Modify: `README.md`（Batch Operations 三塊→四塊）、`CLAUDE.md`（面板規則三個→四個 section + 新 section bullet）

**Interfaces:**
- Consumes（既有，勿改）: `_clean_text(raw, *, lower=False)`、`_looks_english(word)`、`PLACEHOLDERS`、`_deck_note_ids()`、`_section_title(text)`、`_hline()`、`mw.col`（`get_note`/`update_note`/`save`）、`mw.reset()`、`open_backfill_dialog()`、Qt widgets。
- Produces: `_sentence_word_count(html_value) -> int`、`_clamp_length_threshold(raw, default=20) -> int`、`_long_sentence_label(word, count) -> str`、`REBUILD_CLEAR_FIELDS`、`class LongSentencesSection(QWidget)`。

- [ ] **Step 1: 寫失敗測試**

```python
# test_long_sentences.py
"""Batch Operations「Rebuild Long Sentences」section 的純邏輯測試。

Qt/mw.col 層(掃描、清除、跳轉)走手動驗證;這裡測抽出來的決策純函式。
addon 會 import aqt → 用萬用假模組頂替後 import(同 test_backfill_remove.py)。
"""
import sys
import types

import pytest


class _AnyMeta(type):
    def __getattr__(cls, _):
        return _Any()


class _Any(metaclass=_AnyMeta):
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, _):
        return _Any()


def _install_fake_aqt():
    aqt = types.ModuleType("aqt")
    aqt.mw = _Any()
    qt = types.ModuleType("aqt.qt")
    for name in ["QAction", "QDialog", "QVBoxLayout", "QHBoxLayout", "QFormLayout",
                 "QLabel", "QLineEdit", "QPushButton", "QProgressBar", "QScrollArea",
                 "QTreeWidget", "QTreeWidgetItem", "QWidget", "QFrame", "QCheckBox",
                 "QKeySequenceEdit", "QKeySequence", "QMessageBox", "QThread",
                 "pyqtSignal", "Qt"]:
        setattr(qt, name, _Any)
    aqt.qt = qt
    utils = types.ModuleType("aqt.utils")
    utils.showWarning = _Any()
    utils.tooltip = _Any()
    sys.modules["aqt"] = aqt
    sys.modules["aqt.qt"] = qt
    sys.modules["aqt.utils"] = utils


_install_fake_aqt()
import addon  # noqa: E402


class TestSentenceWordCount:
    def test_plain_sentence(self):
        assert addon._sentence_word_count("The quick brown fox jumps.") == 5

    def test_strips_html(self):
        assert addon._sentence_word_count("<div>The <b>quick</b> fox.</div>") == 3

    def test_empty_is_zero(self):
        assert addon._sentence_word_count("") == 0
        assert addon._sentence_word_count("   ") == 0

    def test_placeholder_is_zero(self):
        # 佔位符=「缺句」不是「長句」,回 0 → 永遠不超標
        assert addon._sentence_word_count(
            "Please add an example sentence for 'foo'.") == 0

    def test_long_sentence(self):
        s = " ".join(["word"] * 27)
        assert addon._sentence_word_count(s) == 27


class TestClampLengthThreshold:
    def test_plain_number(self):
        assert addon._clamp_length_threshold("16") == 16

    def test_blank_uses_default(self):
        assert addon._clamp_length_threshold("") == 20

    def test_non_numeric_uses_default(self):
        assert addon._clamp_length_threshold("abc") == 20

    def test_zero_and_negative_clamp_to_one(self):
        assert addon._clamp_length_threshold("0") == 1
        assert addon._clamp_length_threshold("-5") == 1

    def test_big_number_accepted(self):
        assert addon._clamp_length_threshold("999") == 999   # 掃不到東西是合理結果

    def test_strips_whitespace(self):
        assert addon._clamp_length_threshold("  18  ") == 18


class TestLongSentenceLabel:
    def test_format(self):
        assert addon._long_sentence_label("transient", 27) == "transient(27)"


class TestRebuildClearFields:
    def test_exactly_three_sentence_fields(self):
        # 清且只清「句子相關」三欄 — 防手滑加欄位
        assert addon.REBUILD_CLEAR_FIELDS == ["Sentence", "Sentence_CN", "Audio"]
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest test_long_sentences.py -q`
Expected: FAIL/ERROR（`_sentence_word_count` 等不存在，`AttributeError`）

- [ ] **Step 3: 加純函式與常數（`addon/__init__.py`，放在 `_looks_english` 定義之後）**

```python
def _sentence_word_count(html_value):
    """例句欄 → 英文字數(去 HTML)。空句/佔位符回 0 — 那是「缺句」(⌘S 的事),
    不是「長句」,回 0 讓它永遠不超過門檻。"""
    text = _clean_text(html_value or "")
    if not text or any(p in text for p in PLACEHOLDERS) or \
            text.startswith("Please add an example sentence"):
        return 0
    return len(text.split())


def _clamp_length_threshold(raw, default=20):
    """Rebuild Long Sentences 的門檻輸入解析:空白/非數字→default,下限 1,無上限
    (填 999 掃不到東西是合理結果)。"""
    try:
        n = int(str(raw).strip())
    except (ValueError, TypeError):
        return default
    return max(1, n)


def _long_sentence_label(word, count):
    """清單項目樣式:transient(27)。"""
    return f"{word}({count})"


# Rebuild Long Sentences 清除的欄位 — 只清「句子相關」三欄(Audio 是句子語音,隨句連動);
# 保留 Front/Association/Translation/Image_Prompt/Front_Audio(單字層級,不受換句影響)
REBUILD_CLEAR_FIELDS = ["Sentence", "Sentence_CN", "Audio"]
```

（注意：`PLACEHOLDERS` 清單不含 ⌘S 寫的佔位句 `Please add an example sentence for '...'` 的固定前綴 → 用 `startswith` 另擋。實作前先看 `PLACEHOLDERS` 實際內容（`addon/__init__.py:37`），若已涵蓋就不用 startswith——以實際為準，測試已含此案例會抓到。）

- [ ] **Step 4: 跑測試確認綠**

Run: `uv run pytest test_long_sentences.py -q`
Expected: `13 passed`

- [ ] **Step 5: 加 `LongSentencesSection`（放在 `class TestCardsSection` 定義之前）**

```python
class LongSentencesSection(QWidget):
    """Batch Operations section: find sentences longer than a threshold and clear
    their sentence-related fields (Sentence / Sentence_CN / Audio) so the existing
    pipelines regenerate short ones — clearing only, NO generation here (that is
    Complete Missing Cards' / the CLI's job). Synchronous, no worker.
    背景:句長規則(6-12字)是後來才進 prompt 的,舊卡留下大量長句(實測 >20 字 55 張)。"""

    def __init__(self, panel, parent=None):
        super().__init__(parent)
        self._panel = panel          # the Batch Operations dialog, so buttons can close it
        self._hits = []              # [{"nid", "word", "count"}] 字數降冪
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(_section_title("Rebuild Long Sentences"))

        desc = QLabel("Old cards predate the 6-12 word sentence rule. Clears Sentence, "
                      "its translation and its audio for sentences longer than the "
                      "threshold — regenerate them afterwards with Complete Missing Cards.")
        desc.setWordWrap(True)
        root.addWidget(desc)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Longer than:"))
        self.threshold_input = QLineEdit("20")
        self.threshold_input.setFixedWidth(50)
        ctl.addWidget(self.threshold_input)
        ctl.addWidget(QLabel("words"))
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self._scan)
        ctl.addWidget(rescan)
        ctl.addStretch()
        ctl_w = QWidget()
        ctl_w.setLayout(ctl)
        root.addWidget(ctl_w)

        self.word_list = QLabel("")
        self.word_list.setWordWrap(True)
        self.word_list.setStyleSheet("color:#475569; padding:4px;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.word_list)
        scroll.setMinimumHeight(70)
        root.addWidget(scroll)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setStyleSheet("color:#16a34a; font-weight:600;")
        self.status.setVisible(False)
        root.addWidget(self.status)

        # before clearing: single Clear button (right-aligned); the list above + this
        # press is the only gate — no secondary confirm dialog (matches ClearFlagged).
        clear_row = QHBoxLayout()
        clear_row.addStretch()
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setEnabled(False)
        self.clear_btn.clicked.connect(self._on_clear)
        clear_row.addWidget(self.clear_btn)
        self.clear_row_w = QWidget()
        self.clear_row_w.setLayout(clear_row)
        root.addWidget(self.clear_row_w)

        # after clearing: optional jump to Complete Missing Cards (left), or just finish
        post_row = QHBoxLayout()
        self.open_complete_btn = QPushButton("Open Complete Missing Cards")
        self.open_complete_btn.clicked.connect(self._open_complete)
        post_row.addWidget(self.open_complete_btn)
        post_row.addStretch()
        self.done_btn = QPushButton("Done")
        self.done_btn.clicked.connect(self._done)
        post_row.addWidget(self.done_btn)
        self.post_row_w = QWidget()
        self.post_row_w.setLayout(post_row)
        self.post_row_w.setVisible(False)
        root.addWidget(self.post_row_w)

    def _scan(self):
        threshold = _clamp_length_threshold(self.threshold_input.text())
        self.threshold_input.setText(str(threshold))   # 正規化顯示(空白/非數字→預設)
        self._hits = []
        for nid in _deck_note_ids():
            note = mw.col.get_note(nid)
            word = _clean_text(note["Front"])
            if not _looks_english(word):       # 非英文 → 不碰
                continue
            count = _sentence_word_count(note["Sentence"])
            if count > threshold:
                self._hits.append({"nid": nid, "word": word, "count": count})
        self._hits.sort(key=lambda h: h["count"], reverse=True)
        self.status.setVisible(False)
        self.clear_row_w.setVisible(True)
        self.post_row_w.setVisible(False)
        if self._hits:
            self.word_list.setText(" · ".join(
                _long_sentence_label(h["word"], h["count"]) for h in self._hits))
            self.clear_btn.setText(f"Clear {len(self._hits)} Sentences")
            self.clear_btn.setEnabled(True)
        else:
            self.word_list.setText(f"No sentences longer than {threshold} words.")
            self.clear_btn.setText("Clear")
            self.clear_btn.setEnabled(False)

    def _on_clear(self):
        if not self._hits:
            return
        for h in self._hits:
            note = mw.col.get_note(h["nid"])
            for f in REBUILD_CLEAR_FIELDS:       # 只清句子相關三欄
                if f in note:
                    note[f] = ""
            mw.col.update_note(note)
        mw.col.save()
        mw.reset()
        n = len(self._hits)
        self._hits = []
        self.word_list.setText("")
        self.clear_row_w.setVisible(False)
        self.status.setText(f"✓ Cleared {n} long sentence(s) (+ translation & audio). "
                            "Regenerate them now?")
        self.status.setVisible(True)
        self.post_row_w.setVisible(True)

    def _open_complete(self):
        self._panel.accept()         # close the panel, then jump to Complete Missing Cards
        open_backfill_dialog()

    def _done(self):
        self._panel.accept()
```

- [ ] **Step 6: 組裝進面板（`BatchOperationsDialog.__init__`，`addon/__init__.py:1623` 附近，以內容定位）**

現有：

```python
        root.addWidget(TranslateSection(self))
        root.addWidget(_hline())
        root.addWidget(ClearFlaggedSection(self, parent=self))
        root.addWidget(_hline())
        root.addWidget(TestCardsSection(self, parent=self))
        root.addWidget(_hline())
```

改為（LongSentences 插在 ClearFlagged 與 TestCards 之間——功能 section 在前、開發輔助殿後）：

```python
        root.addWidget(TranslateSection(self))
        root.addWidget(_hline())
        root.addWidget(ClearFlaggedSection(self, parent=self))
        root.addWidget(_hline())
        root.addWidget(LongSentencesSection(self, parent=self))
        root.addWidget(_hline())
        root.addWidget(TestCardsSection(self, parent=self))
        root.addWidget(_hline())
```

並把 class docstring 的「sentence-translation backfill on top, clear-flagged in the middle, test-card helper at the bottom」更新為四塊的描述（自行措辭，英文）。

- [ ] **Step 7: 語法檢查 + 全套測試**

Run:
```bash
python3 -c "import ast; ast.parse(open('addon/__init__.py').read()); print('syntax OK')"
uv run pytest -q
```
Expected: `syntax OK`；`201 passed`（188 + 13）

- [ ] **Step 8: 文件同步**

- `README.md` Batch Operations 段（~79 行）「一個面板、上下三塊」→「四塊」，並在 Clear Flagged 與 Test Cards 之間插入：

```markdown
- **中②｜Rebuild Long Sentences（重建過長例句）**：舊卡的例句在「6-12 字」規則進 prompt 之前生成，常常過長。填門檻（預設 20 字）→ Rescan 列出超標卡（`單字(字數)`、降冪）→ **Clear N Sentences** 清空例句／整句翻譯／句子語音三欄（**保留**單字翻譯、圖、單字發音；瞬間完成、不重新生成）→ 一鍵跳 ⌘S 重生短句，或大量時走 CLI
```

（原「中｜Clear Flagged」改「中①」；或依 README 現行體例自行調整編號，總之四塊順序 = Translate → Clear Flagged → Rebuild Long Sentences → Test Cards。）

- `CLAUDE.md`：面板規則行「三個堆疊式 section」→「四個」，順序更新；並在 TestCardsSection bullet 前加一條：

```markdown
- **LongSentencesSection = 找過長例句清空重生（只清不生）**。門檻可調（預設 20，`_clamp_length_threshold`）；字數 `_sentence_word_count`（去 HTML、空句/佔位符回 0 → 缺句歸 ⌘S 管）；清 `REBUILD_CLEAR_FIELDS`（Sentence/Sentence_CN/Audio 三欄，**保留 Translation/Image/Front_Audio**——單字層級不受換句影響）；不碰旗標；清完跳 ⌘S 重生。與 ClearFlaggedSection 同模式：同步、無 worker、列清單即閘門。純函式測試在 `test_long_sentences.py`。
```

- [ ] **Step 9: Commit**

```bash
git add addon/__init__.py test_long_sentences.py README.md CLAUDE.md
git commit -m "feat: Batch Operations 加 Rebuild Long Sentences(過長例句清空重生)"
```
（訊息結尾照慣例加 Co-Authored-By。）

---

## 驗收（對照 spec 成功標準）

1. `uv run pytest -q` 全綠（~201）。
2. 使用者手動（**需重啟 Anki**）：⌘F 見第四 section、Scan 出 ~55 張（transient(27) 應在最上）、Clear 後抽查欄位、跳 ⌘S 重生、門檻改 16 → Rescan 數量增至 ~236 級距。
3. 重生後抽查新句長度在 6-12 字左右。
