# Refill Flagged Cards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Mac-side `Refill Flagged Cards…` action that resets every red-flagged card (keeping Word + Association), regenerates the other six fields via the existing backfill engine, and clears the flag as each card finishes.

**Architecture:** All work is in `addon/__init__.py`. A new `RefillWorker(BackfillWorker)` reuses `BackfillWorker._process_one` (full per-card regeneration) but runs sequentially so a Stop button is responsive. A new `RefillFlaggedDialog` scans `flag:1` cards, clears the six fields in the DB, feeds blanked note-dicts to the worker, and unflags each card on completion. Menu entry + configurable ⌘G shortcut wire it into the Tools menu.

**Tech Stack:** Python, PyQt (aqt), Anki collection API (`mw.col`), AnkiConnect (`updateNoteFields`, already used by `BackfillWorker`).

## Global Constraints

- All on-screen text in this dialog is **English** (final language rule: whole app → English; existing Chinese dialogs converted later, separate task).
- Deck / note type scope: `DECK_NAME` = `My_Daily_English`, `MODEL_NAME` = `English_White_Method` (reuse existing constants).
- Trigger flag: **red only** (`flag:1`).
- Keep fields: `Front`, `Association`. Clear + regenerate: `Sentence`, `Sentence_CN`, `Image_Prompt`, `Audio`, `Front_Audio`, `Translation`.
- Default shortcut: `Ctrl+G` (⌘G on macOS), configurable via existing `SettingsDialog`.
- Addon code imports `aqt` at module top → **cannot be imported under pytest**. No addon unit tests exist; the automated gate per task is `python -m py_compile addon/__init__.py`, and functional verification is **manual in Anki after restart** (project norm: "改完 addon 需重啟 Anki 驗證"). Symlink `addons21/my_word_adder/__init__.py → addon/__init__.py` means editing `addon/__init__.py` is what Anki loads after restart.
- Edit the real file at repo `addon/__init__.py` (never the symlink target directly).
- Commit after each task.

---

### Task 1: RefillWorker (sequential, cancellable regeneration)

**Files:**
- Modify: `addon/__init__.py` — insert a constant and a new class immediately after `BackfillWorker` (after its `run()` method, around line 802, before the `# ── backfill dialog ──` comment at line 804).

**Interfaces:**
- Consumes: `BackfillWorker._process_one(note)` (inherited) — takes a note-dict shaped `{"noteId": int, "fields": {"Front": {"value": str}, "Association": {"value": str}, "Sentence": {"value": str}, "Image_Prompt": {"value": str}, "Audio": {"value": str}, "Front_Audio": {"value": str}, "Translation": {"value": str}, "Sentence_CN": {"value": str}}}`. With all six non-kept fields blank, `_process_one` regenerates them and writes via AnkiConnect, then emits `card_done(note_id)` and returns `"✓ <word>"` / `"✗ <word>: <err>"`. Inherited signals: `step`, `card_done(object)`, `finished(list)`, `error(str)`.
- Produces:
  - `REFILL_CLEAR_FIELDS: list[str]` = the six fields to clear.
  - `class RefillWorker(BackfillWorker)` with: `__init__(self, notes, media_dir)`, `stop(self)`, new signal `progress = pyqtSignal(str, int, int)` (word, current_index_1based, total), and a sequential `run()` that emits `progress` before each card, calls `_process_one`, and stops cleanly when `stop()` was called.

- [ ] **Step 1: Add the field-clear constant and RefillWorker class**

Insert after line 802 (`        self.finished.emit(results)` of `BackfillWorker.run`) and its trailing blank line, before `# ── backfill dialog ──`:

```python
REFILL_CLEAR_FIELDS = ["Sentence", "Sentence_CN", "Image_Prompt",
                       "Audio", "Front_Audio", "Translation"]


class RefillWorker(BackfillWorker):
    """Reset-and-refill flagged cards. Reuses BackfillWorker._process_one (full
    regeneration of every blank field) but processes one card at a time so the
    Stop button is responsive. Notes are passed in already blanked, so every
    field regenerates."""

    progress = pyqtSignal(str, int, int)   # word, current_index (1-based), total

    def __init__(self, notes, media_dir):
        super().__init__(notes, media_dir)
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        results = []
        total = len(self.notes)
        for i, note in enumerate(self.notes, 1):
            if self._stop:
                break
            word = _clean_text(note["fields"]["Front"]["value"], lower=True)
            self.progress.emit(word, i, total)
            try:
                results.append(self._process_one(note))
            except Exception as e:
                results.append(f"✗ {word}: {e}")
        self.finished.emit(results)
```

- [ ] **Step 2: Syntax gate**

Run: `cd /Users/yeqilin/Workspace/anki && python -m py_compile addon/__init__.py && echo OK`
Expected: prints `OK`, no traceback.

- [ ] **Step 3: Commit**

```bash
cd /Users/yeqilin/Workspace/anki
git add addon/__init__.py
git commit -m "feat: RefillWorker — sequential cancellable refill (reuses BackfillWorker)"
```

---

### Task 2: RefillFlaggedDialog

**Files:**
- Modify: `addon/__init__.py` — insert a new dialog class after `RefillWorker` (i.e. after Task 1's class, still before `# ── backfill dialog ──`, or immediately after `BackfillDialog` ends at line 933 — place it right after `BackfillDialog` so related dialogs sit together; either location compiles, prefer after `BackfillDialog`).

**Interfaces:**
- Consumes: `RefillWorker` (Task 1), `REFILL_CLEAR_FIELDS` (Task 1), `_deck_note_ids` neighbours `DECK_NAME`/`MODEL_NAME`, `_clean_text`, `mw.col.find_cards`, `mw.col.get_card(cid).nid`, `mw.col.get_note(nid)`, `mw.col.update_note(note)`, `mw.col.set_user_flag_for_cards(flag, card_ids)`, `mw.col.media.dir()`, `mw.col.save()`, `mw.reset()`.
- Produces: `class RefillFlaggedDialog(QDialog)` with no-arg-besides-parent constructor `RefillFlaggedDialog(mw).exec()`.

- [ ] **Step 1: Add the dialog class**

Insert immediately after `BackfillDialog._on_finished` ends (line 933, the blank line before `# ── find duplicates dialog ──` at line 935):

```python
# ── refill flagged dialog ──────────────────────────────────────────────────────

class RefillFlaggedDialog(QDialog):
    """Reset every red-flagged card (keep Word + Association, clear & regenerate the
    rest) and clear the flag as each finishes. Flagging is done on the phone with
    Anki's built-in red flag; this is the Mac-side processor."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Refill Flagged Cards")
        self.setMinimumWidth(600)
        self._worker = None
        self._flagged = []      # [{"nid", "cids", "word"}]
        self._card_ids = {}     # note_id -> [card_id, ...] for unflagging
        self._setup_ui()
        self._scan()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        desc = QLabel("Word and Association are kept. All other fields (sentence, "
                      "both translations, image, word audio, sentence audio) are "
                      "cleared and regenerated.")
        desc.setWordWrap(True)
        root.addWidget(desc)

        self.word_list = QLabel("")
        self.word_list.setWordWrap(True)
        self.word_list.setStyleSheet("color:#475569; padding:4px;")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.word_list)
        scroll.setMinimumHeight(80)
        root.addWidget(scroll)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        btns = QHBoxLayout()
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        self.start_btn = QPushButton("Start")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self._on_start)
        btns.addWidget(self.stop_btn)
        btns.addWidget(self.start_btn)
        root.addLayout(btns)

    def _scan(self):
        self._flagged = []
        cids = mw.col.find_cards(f'deck:"{DECK_NAME}" note:"{MODEL_NAME}" flag:1')
        by_note = {}
        for cid in cids:
            nid = mw.col.get_card(cid).nid
            by_note.setdefault(nid, []).append(cid)
        words = []
        for nid, cardids in by_note.items():
            note = mw.col.get_note(nid)
            word = _clean_text(note["Front"])
            self._flagged.append({"nid": nid, "cids": cardids, "word": word})
            words.append(word)
        if self._flagged:
            self.word_list.setText(" · ".join(words))
            self.start_btn.setEnabled(True)
        else:
            self.word_list.setText("No flagged cards.")
            self.start_btn.setEnabled(False)

    def _on_start(self):
        if not self._flagged:
            return
        notes = []
        self._card_ids = {}
        for item in self._flagged:
            nid = item["nid"]
            self._card_ids[nid] = item["cids"]
            note = mw.col.get_note(nid)
            for f in REFILL_CLEAR_FIELDS:
                if f in note:
                    note[f] = ""
            mw.col.update_note(note)
            notes.append({
                "noteId": nid,
                "fields": {
                    "Front":        {"value": note["Front"]},
                    "Association":  {"value": note["Association"]},
                    "Sentence":     {"value": ""},
                    "Image_Prompt": {"value": ""},
                    "Audio":        {"value": ""},
                    "Front_Audio":  {"value": ""},
                    "Translation":  {"value": ""},
                    "Sentence_CN":  {"value": ""},
                },
            })
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, len(notes))
        self.progress_bar.setValue(0)
        self._worker = RefillWorker(notes, mw.col.media.dir())
        self._worker.progress.connect(self._on_progress)
        self._worker.card_done.connect(self._on_card_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(lambda e: self.status.setText(f"Error: {e}"))
        self._worker.start()

    def _on_progress(self, word, i, total):
        self.progress_bar.setValue(i)
        self.status.setText(f"Refilling: {word} ({i}/{total})")

    def _on_card_done(self, note_id):
        cids = self._card_ids.get(note_id)
        if cids:
            mw.col.set_user_flag_for_cards(0, cids)

    def _on_stop(self):
        if self._worker:
            self._worker.stop()
        self.stop_btn.setEnabled(False)
        self.status.setText("Stopping…")

    def _on_finished(self, results):
        mw.col.save()
        mw.reset()
        self.progress_bar.setVisible(False)
        self.stop_btn.setEnabled(False)
        ok = sum(1 for r in results if r.startswith("✓"))
        self.status.setText(f"Refilled {ok} card(s). Remember to sync Anki!")
        self._scan()   # refilled cards are unflagged now → list shrinks / empties
```

- [ ] **Step 2: Syntax gate**

Run: `cd /Users/yeqilin/Workspace/anki && python -m py_compile addon/__init__.py && echo OK`
Expected: prints `OK`, no traceback.

- [ ] **Step 3: Commit**

```bash
cd /Users/yeqilin/Workspace/anki
git add addon/__init__.py
git commit -m "feat: RefillFlaggedDialog — scan red-flagged cards, clear & refill, unflag"
```

---

### Task 3: Menu entry, shortcut, settings label, end-to-end verification

**Files:**
- Modify: `addon/__init__.py`
  - Add handler near the other `open_*` handlers (after `open_sentence_cn_dialog` at line 1256).
  - `DEFAULT_SHORTCUTS` (line 1258-1259): add `"refill_flagged": "Ctrl+G"`.
  - `_add_menu_action` calls (after line 1350 `Complete Missing Cards…`): add the new action so it sits right under Complete.
  - `SettingsDialog.LABELS` (line 1282-1287): add `("refill_flagged", "Refill Flagged Cards")`.

**Interfaces:**
- Consumes: `RefillFlaggedDialog` (Task 2), `_add_menu_action`, `DEFAULT_SHORTCUTS`, `SettingsDialog.LABELS`.
- Produces: a Tools-menu item `Refill Flagged Cards…` bound to ⌘G (configurable).

- [ ] **Step 1: Add the handler**

After `open_sentence_cn_dialog` (line 1256), add:

```python
def open_refill_flagged_dialog():
    RefillFlaggedDialog(mw).exec()
```

- [ ] **Step 2: Register the default shortcut**

Change (line 1258-1259):

```python
DEFAULT_SHORTCUTS = {"add": "Ctrl+D", "complete": "Ctrl+S", "find_duplicates": "Ctrl+F",
                     "backfill_cn": "Ctrl+B"}
```

to:

```python
DEFAULT_SHORTCUTS = {"add": "Ctrl+D", "complete": "Ctrl+S", "find_duplicates": "Ctrl+F",
                     "backfill_cn": "Ctrl+B", "refill_flagged": "Ctrl+G"}
```

- [ ] **Step 3: Add the menu action**

After line 1350 (`_add_menu_action("Complete Missing Cards…", "complete", open_backfill_dialog)`), add:

```python
_add_menu_action("Refill Flagged Cards…", "refill_flagged", open_refill_flagged_dialog)
```

- [ ] **Step 4: Add the settings label**

In `SettingsDialog.LABELS` (line 1282-1287), add after the `backfill_cn` entry:

```python
        ("refill_flagged", "Refill Flagged Cards"),
```

- [ ] **Step 5: Syntax gate**

Run: `cd /Users/yeqilin/Workspace/anki && python -m py_compile addon/__init__.py && echo OK`
Expected: prints `OK`, no traceback.

- [ ] **Step 6: Manual end-to-end verification in Anki**

Anki must be running with AnkiConnect enabled (the backfill engine writes via AnkiConnect).

1. Restart Anki (loads the edited addon via the symlink).
2. Open Browse, find one `My_Daily_English` card that already has full content, set its **red flag** (right-click → Flag → Flag 1, or the flag toolbar). Note its word.
3. Tools menu → confirm `Refill Flagged Cards…` appears under `Complete Missing Cards…`; press **⌘G**.
   - Expected: dialog opens, the flagged word appears in the list, `Start` enabled.
4. Press **Start**.
   - Expected: progress bar advances, status shows `Refilling: <word> (1/1)`, then `Refilled 1 card(s). Remember to sync Anki!`. The list refreshes to `No flagged cards.`
5. In Browse, open the card:
   - Expected: `Front` and `Association` unchanged; `Sentence`, `Sentence_CN`, `Image_Prompt` (image), `Audio`, `Front_Audio`, `Translation` all regenerated with fresh content; the **red flag is cleared**.
6. Empty-state check: with no flagged cards, open the dialog again → list shows `No flagged cards.`, `Start` is disabled.
7. Stop check: flag 3+ cards, Start, then press **Stop** mid-run.
   - Expected: already-finished cards are refilled and unflagged; not-yet-processed cards keep their red flag and (since cleared first) may be blank — reopening the dialog lists those remaining flagged cards so a second Start finishes them.

- [ ] **Step 7: Commit**

```bash
cd /Users/yeqilin/Workspace/anki
git add addon/__init__.py
git commit -m "feat: wire Refill Flagged Cards menu entry + ⌘G shortcut + settings label"
```

---

## Post-implementation

- Update `README.md` (menu list / shortcuts) and `CLAUDE.md` (note the phone-flag → Mac-refill workflow and the RefillWorker reuse-of-_process_one pitfall) per the Pre-push Checklist. Do this as a final `docs:` commit after manual verification passes.
- Do **not** push or treat as done until the user has restarted Anki and confirmed the flow (memory: don't commit/finalize UI before user approves — the per-task commits are local; hold push for user sign-off).

## Notes for the implementer

- `mw.col.set_user_flag_for_cards(flag, card_ids)` takes `flag=0` to clear. Flags are per **card**; this note type has one card per note, but the code maps note→card-ids generically so multi-template notes still unflag correctly.
- Clearing the six fields in `_on_start` **before** running the worker is deliberate: it guarantees the old bad content is gone even if a regeneration call fails (a failed field just stays blank rather than keeping stale text). Passing blanked note-dicts is what makes `_process_one` treat every field as missing and regenerate it.
- `RefillWorker` runs cards sequentially (unlike `BackfillWorker`'s 3-way `ThreadPoolExecutor`) so Stop takes effect after the current card. Flagged batches are small (hand-picked), so throughput is a non-issue.
```
