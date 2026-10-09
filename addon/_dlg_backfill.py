"""⌘S Complete Missing Cards 視窗：`FieldRow`、`BackfillDialog` 與它的純函式。"""

import os

from aqt import mw
from aqt.qt import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QProgressBar, QScrollArea, QWidget, QCheckBox, QKeySequence, QPixmap, Qt,
)
from . import _llm_dispatch as _lld
from ._config import BACKFILL_BOXES, BOX_STYLE, PLACEHOLDERS, _BADGE_OK_STYLE, _BADGE_WARN_STYLE, _FIELD_LABEL, _shortcut
from ._text import _clean_text, _image_filename, _looks_english, _preview_html, _reasons_text
from ._batch import _BatchDialogMixin, _batch_acquire, _batch_busy_message, _batch_release, _deck_note_ids, _live_note, _show_nonmodal, _sync_after_batch
from ._workers import BackfillWorker


THUMB_HEIGHT = 48   # ⌘S 補完那列的縮圖高度（px）


class FieldRow(QWidget):
    """One card's progress: word + Sentence/Audio/Image/Meaning/Translation boxes + 'added!' badge.
    Fields already present start green; missing ones start grey and flip on completion."""

    def __init__(self, word, present, parent=None):
        super().__init__(parent)
        self.word = word
        self._boxes = {}
        self._reasons = {}                # field key → 退回原因短句（只顯示短句；細節在 log）；set_box 會用到，要在迴圈前
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 2, 4, 2)
        self.checkbox = QCheckBox()       # left-most: pick which cards to complete (default unchecked)
        lay.addWidget(self.checkbox)
        self.word_label = wl = QLabel(word)
        wl.setMinimumWidth(120)
        wl.setStyleSheet("font-weight:600; color:#1E293B;")
        wl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)   # 單字可以選取複製（QLabel 預設不行）
        lay.addWidget(wl)
        for key, _label in BACKFILL_BOXES:    # all five fields, incl. the sentence translation
            box = QLabel()
            box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            box.setFixedWidth(90)         # uniform box width regardless of label length
            self._boxes[key] = box
            lay.addWidget(box)
            self.set_box(key, "ok" if present.get(key) else "working")
        self.thumb = QLabel()             # 補完後才出現的縮圖（set_preview）
        self.thumb.setVisible(False)
        lay.addWidget(self.thumb)
        self.badge = QLabel("")
        self.badge.setStyleSheet(_BADGE_OK_STYLE)
        self.badge.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)   # 退回原因也能複製
        lay.addWidget(self.badge)
        lay.addStretch()

    def set_box(self, key, state, reason=""):
        box = self._boxes.get(key)
        if not box:
            return
        style, fmt = BOX_STYLE[state]
        box.setStyleSheet(style)
        box.setText(fmt.format(_FIELD_LABEL[key]))
        if state == "warn" and reason:
            self._reasons[key] = reason
            box.setToolTip(reason)
        else:
            self._reasons.pop(key, None)
            box.setToolTip("")
        if self._reasons:
            self.badge.setStyleSheet(_BADGE_WARN_STYLE)
            self.badge.setText(_reasons_text(self._reasons))

    def set_done(self):
        if self._reasons:                 # 有欄位被退就留著原因，不蓋成 added!
            return
        self.badge.setStyleSheet(_BADGE_OK_STYLE)
        self.badge.setText(f"'{self.word}' added!")

    def set_preview(self, pixmap, tooltip_html):
        """補完的卡：列尾放縮圖，整列 tooltip 放大圖＋文字。橘框自己的 tooltip（退回原因）
        優先——Qt 先問滑鼠底下的元件，它沒有 tooltip 才往上交給整列。"""
        if pixmap is not None and not pixmap.isNull():
            self.thumb.setPixmap(pixmap.scaledToHeight(THUMB_HEIGHT,
                                 Qt.TransformationMode.SmoothTransformation))
            self.thumb.setVisible(True)
        self.setToolTip(tooltip_html)

    def is_checked(self):
        return self.checkbox.isChecked()


def _note_incomplete(note):
    """True if the note is still missing ANY auto-filled field (Sentence / Audio /
    Image / Meaning / Translation). Single source of 'is this card done' — used both by
    the scan and the post-run count, so a card still missing only the translation
    correctly counts as not done (not 'done because we wrote something')."""
    bad_sentence = any(p in note["Sentence"] for p in PLACEHOLDERS)
    front_audio = note["Front_Audio"] if "Front_Audio" in note else ""
    translation = note["Translation"] if "Translation" in note else ""
    sentence_cn = note["Sentence_CN"] if "Sentence_CN" in note else ""
    return (not note["Sentence"] or bad_sentence or not note["Audio"]
            or "<img" not in note["Image_Prompt"] or not front_audio
            or not translation or not sentence_cn)


def _note_snapshot(note):
    """Anki Note -> the field-dict shape BackfillWorker expects, read fresh from
    mw.col.get_note() right now (main thread only — collection access must stay off
    worker threads). Used by `_on_run` so a second Complete click always re-reads
    current field values instead of trusting a snapshot taken when the dialog opened
    (that staleness is what let a rate-limited retry overwrite a good sentence with a
    placeholder — see _sentence_to_write). Notes already complete are harmless to pass
    through: BackfillWorker's need_* checks skip everything for them."""
    front_audio = note["Front_Audio"] if "Front_Audio" in note else ""
    translation = note["Translation"] if "Translation" in note else ""
    sentence_cn = note["Sentence_CN"] if "Sentence_CN" in note else ""
    return {
        "noteId": note.id,
        "fields": {
            "Front":        {"value": note["Front"]},
            "Association":  {"value": note["Association"]},
            "Sentence":     {"value": note["Sentence"]},
            "Image_Prompt": {"value": note["Image_Prompt"]},
            "Audio":        {"value": note["Audio"]},
            "Front_Audio":  {"value": front_audio},
            "Translation":  {"value": translation},
            "Sentence_CN":  {"value": sentence_cn},
        }
    }


# ── backfill dialog ───────────────────────────────────────────────────────────

def _finished_ids(note_ids, lookup):
    """Which of these rows came back complete — the pure decision behind Remove Finished.

    `lookup(note_id)` returns the live note, or None when the card is gone. A card
    counts as finished when it exists and `_note_incomplete` says nothing is missing,
    or when it was deleted elsewhere (it can neither be filled nor needs to be).

    判定一定要重讀欄位,**不能**拿 worker 的 `card_done` 訊號當依據:card_done 的語意是
    「這張處理完了」不是「這張補齊了」——某個欄位生失敗時 helper 靜默回空字串、不 raise,
    照樣跑到最後 emit card_done。拿它當依據,半成品會被當成完成而從清單上消失
    （紅旗卡 Refill 就是這樣把自己的待辦清單擦掉的,見 CLAUDE.md）。"""
    done = set()
    for nid in note_ids:
        note = lookup(nid)
        if note is None or not _note_incomplete(note):
            done.add(nid)
    return done


def _drop_notes(pending_notes, remove_ids):
    """Return pending_notes minus the given note ids — a fresh list, input untouched.
    Pure decision behind Remove Finished (view-only removal; cards stay in Anki)."""
    remove_ids = set(remove_ids)
    return [n for n in pending_notes if n["noteId"] not in remove_ids]


def _removal_status(removed, remaining):
    """Status line after Remove Finished drops the completed rows (view only)."""
    if remaining:
        return (f"Removed {removed} finished card(s). {remaining} still need filling — "
                f"still selected, so Complete Selected picks them up.")
    return "All finished cards cleared from the list."


class BackfillDialog(_BatchDialogMixin, QDialog):
    _DM_NAME = "WhiteForgeBackfill"
    _BATCH_LABEL = "Complete Missing Cards"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Complete Missing Cards")
        self.setMinimumWidth(960)        # room for word col + 5 boxes + two short reject reasons on one row
        self.setMinimumHeight(380)
        self._worker = None
        self._rows = {}
        self._finished = set()      # 跑完一批後重讀欄位算出來的「已補齊」note id
        self._setup_ui()
        self._scan()

    def reopen(self):
        """單例被叫回前面時 Anki 的 dialog manager 會呼叫這裡 → 重掃一次。
        不重掃的話「⌘F 清空紅旗卡 → Open Complete Missing Cards」會看到清空前的
        舊清單，剛清空的卡不在裡面，一鍵重生等於沒作用。批次還在跑就不動
        （重掃會把正在更新的列表整片換掉）。"""
        if self._batch_active():
            return
        self._clear_rows()
        self._finished = set()
        self.remove_btn.setVisible(False)
        self.select_all.setChecked(False)
        self._scan()

    def _clear_rows(self):
        """清掉列表所有 widget 與 stretch —— 重掃前必清,否則新舊清單疊加。"""
        self._rows = {}
        while self._rows_box.count():
            item = self._rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.addWidget(QLabel("Cards missing Sentence / Audio / Image / Meaning / Translation:"))

        self.select_all = QCheckBox("Select all")
        self.select_all.stateChanged.connect(self._on_select_all)
        root.addWidget(self.select_all)

        self._rows_host = QWidget()
        self._rows_box = QVBoxLayout(self._rows_host)
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._rows_host)
        root.addWidget(scroll)

        self.status = QLabel("")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(self.status)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setVisible(False)
        root.addWidget(self.progress_bar)

        btns = QHBoxLayout()
        self.run_btn = QPushButton("Complete Selected (0)")
        self.run_btn.setEnabled(False)
        self.run_btn.clicked.connect(self._on_run)
        self.remove_btn = QPushButton("Remove Finished (0)")
        self.remove_btn.setEnabled(False)
        self.remove_btn.setVisible(False)     # only appears after a batch finishes
        self.remove_btn.clicked.connect(self._on_remove_finished)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(self.run_btn)
        btns.addWidget(self.remove_btn)
        btns.addWidget(close_btn)
        root.addLayout(btns)

    def _scan(self):
        notes = []
        invalid = 0
        for nid in _deck_note_ids():
            note = mw.col.get_note(nid)
            bad_sentence = any(p in note["Sentence"] for p in PLACEHOLDERS)
            front_audio = note["Front_Audio"] if "Front_Audio" in note else ""
            translation = note["Translation"] if "Translation" in note else ""
            sentence_cn = note["Sentence_CN"] if "Sentence_CN" in note else ""
            has_img = "<img" in note["Image_Prompt"]
            audio_ok = bool(note["Audio"]) and bool(front_audio)
            if not _note_incomplete(note):
                continue

            word = _clean_text(note["Front"])
            if not _looks_english(word):       # not English → don't fill, just flag it
                invalid += 1
                lbl = QLabel(f"{word or note['Front']} (contains non-English characters, cannot be created)")
                lbl.setStyleSheet("color:#ea580c; padding:4px;")
                self._rows_box.addWidget(lbl)
                continue

            present = {
                "sentence": bool(note["Sentence"]) and not bad_sentence,
                "image": has_img,
                "audio": audio_ok,
                "translation": bool(translation),
                "sentence_cn": bool(sentence_cn),
            }
            row = FieldRow(word, present)
            row.checkbox.stateChanged.connect(lambda *_: self._update_selection())
            self._rows_box.addWidget(row)
            self._rows[nid] = row
            notes.append({
                "noteId": nid,
                "fields": {
                    "Front":        {"value": note["Front"]},
                    "Association":  {"value": note["Association"]},
                    "Sentence":     {"value": note["Sentence"]},
                    "Image_Prompt": {"value": note["Image_Prompt"]},
                    "Audio":        {"value": note["Audio"]},
                    "Front_Audio":  {"value": front_audio},
                    "Translation":  {"value": translation},
                    "Sentence_CN":  {"value": sentence_cn},
                }
            })
        self._rows_box.addStretch()
        self._pending_notes = notes
        parts = []
        if notes:
            parts.append(f"{len(notes)} card(s) need filling.")
        if invalid:
            parts.append(f"{invalid} card(s) contain non-English characters and cannot be created (please fix or delete).")
        self.status.setText(" ".join(parts) if parts else "All cards are complete!")
        self.select_all.setEnabled(bool(notes))
        # 預設全選:日常用法就是「開窗、按一下、全部補完」,要挑掉某張卡再自己取消勾選。
        # 直接呼叫 _on_select_all 而不是靠 setChecked 的 stateChanged —— 值沒變時
        # Qt 不會 emit,reopen 進來若 select_all 已是 True,新掃出來的列就會漏勾。
        self.select_all.setChecked(bool(notes))
        self._on_select_all()
        self._update_selection()

    def _update_selection(self):
        n = sum(1 for r in self._rows.values() if r.is_checked())
        self.run_btn.setText(f"Complete Selected ({n})")
        self.run_btn.setEnabled(n > 0)

    def _update_remove_button(self):
        """Remove Finished 只算補齊的卡,跟打勾無關 —— 開窗預設全選,綁勾選等於
        「按一下清空整個清單」,連還沒補完的也看不到了。"""
        n = len(self._finished & self._rows.keys())
        self.remove_btn.setText(f"Remove Finished ({n})")
        self.remove_btn.setEnabled(n > 0)

    def _on_select_all(self, state=None):
        checked = self.select_all.isChecked()
        for row in self._rows.values():
            row.checkbox.setChecked(checked)

    def _on_run(self):
        selected = [n for n in self._pending_notes
                    if (r := self._rows.get(n["noteId"])) and r.is_checked()]
        if not selected:
            return
        if not _batch_acquire(self._BATCH_LABEL):     # 別的批次在跑 → 會互搶額度/重複寫卡
            self.status.setText(_batch_busy_message())
            return
        # Re-read current field values now (not self._pending_notes, a snapshot from when
        # the dialog opened) — a second Complete click must not think fields are still
        # missing just because they were missing when the dialog was first opened.
        # 視窗非阻塞後卡片可能已被別處刪掉 → _live_note 跳過死掉的 id。
        fresh_notes = [_note_snapshot(note) for n in selected
                       if (note := _live_note(n["noteId"])) is not None]
        if not fresh_notes:
            _batch_release(self._BATCH_LABEL)
            self.status.setText("Those cards no longer exist — reopen this window to rescan.")
            return
        self.run_btn.setEnabled(False)
        self.select_all.setEnabled(False)
        self.progress_bar.setVisible(True)
        self._worker = BackfillWorker(fresh_notes, mw.col.media.dir())
        self._worker.step.connect(self._on_step)
        self._worker.card_done.connect(self._on_card_done)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(lambda e: self.status.setText(f"Error: {e}"))
        self._worker.start()

    def _on_step(self, note_id, field, state, reason=""):
        row = self._rows.get(note_id)
        if row:
            row.set_box(field, state, reason)

    def _on_card_done(self, note_id):
        row = self._rows.get(note_id)
        if row:
            row.set_done()
            self._show_preview(row, note_id)

    def _show_preview(self, row, note_id):
        """重讀欄位（不信 worker 回報的值）→ 縮圖＋浮動預覽。卡被刪了就不顯示；圖讀不到只放文字。"""
        note = _live_note(note_id)
        if note is None:
            return
        name = _image_filename(note["Image_Prompt"])
        path = os.path.join(mw.col.media.dir(), name) if name else ""
        if path and not os.path.exists(path):
            path = ""
        pixmap = QPixmap(path) if path else None
        size = (pixmap.width(), pixmap.height()) if pixmap is not None else (0, 0)
        row.set_preview(pixmap,
                        _preview_html(note["Sentence"], note["Sentence_CN"] if "Sentence_CN" in note else "",
                                      note["Translation"] if "Translation" in note else "", path, size))

    def _on_finished(self, results):
        self.progress_bar.setVisible(False)
        mw.col.save()
        mw.reset()
        _sync_after_batch()
        # 已被刪掉的卡（非阻塞視窗開著時可能被別處刪除）兩邊都不算：不算「還缺」,
        # 也不算「已完成」——只從 total 扣掉。否則 3 張選取、跑到一半刪掉 1 張,
        # 會顯示「3 card(s) completed」而實際只做了 2 張。
        live = [note for n in self._worker.notes
                if (note := _live_note(n["noteId"])) is not None]
        total = len(live)
        left = sum(1 for note in live if _note_incomplete(note))
        done = total - left
        if getattr(self._worker, "_stopped", False):
            self.status.setText(f"Stopped — completed {done}, {left} still need filling.")
        elif getattr(self._worker, "_hit_limit", False):
            secs = int(self._worker.retry_after)
            resets = getattr(self._worker, "limit_resets", {})
            pools = getattr(self._worker, "limit_pools", [])
            if resets:
                label = ("Sentence" if pools == ["sentence"] else
                         "Translation" if pools == ["light"] else "All")
                self.status.setText(
                    f"{label} models are out of quota — {_lld.format_reset_summary(resets)}. "
                    f"Completed {done}, {left} still need filling.")
            else:                      # 沒有池資訊:沿用原措辭
                self.status.setText(
                    f"Hit the cloud rate limit — completed {done}, {left} still need "
                    f"filling. Try again in ~{secs}s, then reselect.")
        elif left:
            self.status.setText(f"Completed {done}, {left} still need filling — some fields "
                                f"didn't come back, try those again.")
        else:
            self.status.setText(f"Done — {done} card(s) completed.")
        # 重讀每一列的欄位算出誰補齊了 —— Remove Finished 只拿掉這些,沒補完的留在
        # 清單上而且勾還在,額度回來直接再按 Complete Selected 續跑。
        self._finished = _finished_ids(list(self._rows), _live_note)
        new_terms = set(getattr(self._worker, "new_pending_terms", []))
        if new_terms:                  # 翻譯被驗證丟掉的英文片語已記成待審 → 提示去審
            sc = _shortcut("terms")
            native = QKeySequence(sc).toString(QKeySequence.SequenceFormat.NativeText) if sc else ""
            where = f" ({native})" if native else ""      # macOS 上顯示 ⌘D 而不是 Ctrl+D
            self.status.setText(self.status.text() +
                                f" {len(new_terms)} new term(s) to review in Translation Terms{where}.")
        self.select_all.setEnabled(True)
        self.remove_btn.setVisible(True)
        self._update_selection()
        self._update_remove_button()
        self._end_batch()

    def _on_remove_finished(self):
        """Drop the rows that came back complete — view only. Those cards stay in Anki;
        this only clears them off the dialog. Rows still missing fields stay listed
        and stay checked, so the next Complete Selected picks them up without a rescan."""
        to_remove = [nid for nid in self._rows if nid in self._finished]
        if not to_remove:
            return
        for nid in to_remove:                        # Qt side: drop the row widgets
            row = self._rows.pop(nid)
            self._rows_box.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._pending_notes = _drop_notes(self._pending_notes, to_remove)
        self._finished -= set(to_remove)
        self.status.setText(_removal_status(len(to_remove), len(self._rows)))
        if not self._rows:                           # list emptied → retire the button
            self.remove_btn.setVisible(False)
            self.select_all.setEnabled(False)
        self._update_selection()
        self._update_remove_button()


def open_backfill_dialog():
    _show_nonmodal(BackfillDialog)
