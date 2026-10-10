"""批次視窗的共用基礎：批次互斥、`_live_note`、寫完卡片自動同步、`_BatchDialogMixin`、非阻塞開窗。"""

import math
import threading
import time
import aqt
from aqt import mw
from aqt.qt import (
    QLabel, QFrame, QTimer, Qt, QScrollArea, QWidget, QVBoxLayout, QHBoxLayout,
    QPainter, QPalette, QPen, QRectF,
)
from . import _llm_dispatch as _lld
from ._config import DECK_NAME, MODEL_NAME

_log = _lld.get_logger()   # 批次/LLM 事件集中記錄到 logs/addon_llm.log（gitignored）


def _deck_note_ids():
    """Note ids in the deck restricted to our note type, so deck scans never touch a
    stray note type (e.g. a Cloze card) that lacks our fields and would KeyError."""
    return mw.col.find_notes(f'deck:"{DECK_NAME}" note:"{MODEL_NAME}"')


# ── 非阻塞視窗的三道防線 ──────────────────────────────────────────────────────
# 批次視窗改用非阻塞 show()（不鎖 Anki）之後，exec() 原本隱性提供的保護沒了，
# 這裡逐一補回：批次互斥、關窗不留殭屍 worker、卡片被別處刪掉的防呆。

_batch_owner = None                  # 目前在跑批次的視窗名稱（None = 沒有）


_batch_lock = threading.Lock()


def _batch_acquire(label):
    """取得批次權；別的視窗正在跑就回 False（呼叫端負責告知使用者）。
    為何要互斥：兩個批次同時跑會搶同一份速率額度，而且 ⌘S 與 ⌘F 都會寫
    Sentence_CN → 對同一批卡的同一欄位重複寫、互相蓋掉。"""
    global _batch_owner
    with _batch_lock:
        if _batch_owner is not None and _batch_owner != label:
            return False
        _batch_owner = label
        return True


def _batch_release(label):
    """釋放批次權；只有持有者能釋放（別的視窗收尾不該解掉別人的鎖）。"""
    global _batch_owner
    with _batch_lock:
        if _batch_owner == label:
            _batch_owner = None


def _batch_busy():
    return _batch_owner


def _batch_busy_message():
    return (f"A batch is already running in {_batch_owner}. "
            f"Wait for it to finish, then try again.")


def _blocked_by_batch(show_message):
    """跑批中不准動卡片：清空欄位 / 建卡 / 刪卡會和正在寫同一批卡的 worker 打架。
    擋下來就回 True（並用傳進來的函式把原因顯示給使用者）。"""
    if _batch_busy() is None:
        return False
    show_message(_batch_busy_message())
    return True


def _live_note(note_id):
    """note 還在就回它，已被刪掉回 None。
    非阻塞視窗開著時卡片可能被別處刪掉（⌘F Clean Test Cards / 刪重複、Browse），
    `mw.col.get_note()` 會拋 NotFoundError → 呼叫端跳過，而不是讓例外冒到 Anki。"""
    try:
        return mw.col.get_note(note_id)
    except Exception:
        return None


def _auto_sync_allowed(logged_in, media_syncing, progress_busy, batch_owner):
    """批次寫完卡片後，現在能不能自動同步（純函式，測試在 test_auto_sync.py）。

    - 沒登入 AnkiWeb：不同步，也不跳登入框（自動動作不該冒出要輸入密碼的視窗）。
    - 媒體還在同步：此時按同步鍵 Anki 會改開「媒體同步紀錄」視窗，不是同步。
    - 已有進度視窗（例如上一次同步還沒完）：等它，不疊第二個。
    - 又有批次在跑：worker 正在寫卡，等它收尾時自己會再觸發一次同步。"""
    return logged_in and not media_syncing and not progress_busy and batch_owner is None


def _sync_after_batch():
    """⌘A／⌘S／⌘F 寫完卡片後自動同步 AnkiWeb，Mac 這邊不用再手按同步。
    （手機那邊 addon 管不到：AnkiMobile 沒有自動同步，見 README。）

    「瀏覽」視窗開著也照同步，不用等它關：Anki 自己的同步收尾會呼叫 mw.reset()，
    瀏覽視窗的編輯器因此從資料庫重新載入卡片，不會像 AnkiConnect 外部寫入那樣
    被編輯器手上的舊內容蓋回去（Anki 26.09 原始碼 main.py `_sync_collection_and_media`
    → browser.py `on_operation_did_execute`）。

    排到事件迴圈下一輪才跑：呼叫端多半還在收尾（_end_batch 釋放批次權、可能正要關窗）。"""
    QTimer.singleShot(0, _sync_now)


def _sync_now():
    try:
        allowed = _auto_sync_allowed(
            logged_in=bool(mw.pm.sync_auth()),
            media_syncing=mw.media_syncer.is_syncing(),
            progress_busy=mw.progress.busy(),
            batch_owner=_batch_busy(),
        )
        if allowed:
            mw.on_sync_button_clicked()
        else:
            _log.info("auto-sync skipped (not logged in, or busy)")
    except Exception as e:            # 自動同步失敗不能打斷使用者；手按同步仍可用
        _log.warning("auto-sync failed: %s", e)


class _BatchDialogMixin:
    """批次視窗共用：Anki dialog-manager 契約 + 跑批中不關窗。

    - `done()`（Close / X / accept 都會走到）在批次跑到一半時只「請 worker 停」，
      不真的關窗——視窗一關，worker 的 signal 就打到已刪除的 Qt 物件，而且
      `_on_finished` 還會在背後 `mw.col.save()` / `mw.reset()`。worker 收尾時
      各 dialog 呼叫 `_end_batch()`，那時才真的關。
    - `closeWithCallback()`：Anki 退出 / 切 profile 時 `aqt.dialogs.closeAll` 會呼叫，
      先停批並等 thread 真的結束，才放 Anki 繼續卸載 collection。
    """
    _DM_NAME = None          # aqt.dialogs 註冊名（子類覆寫）
    _BATCH_LABEL = None      # 批次互斥用的顯示名（子類覆寫）
    # 退出時等 worker 收尾的上限。取 15s = 單次 LLM 呼叫的 timeout：手上那張卡的
    # 網路呼叫最多就這麼久，等掉它才不會對正在卸載的 collection 寫入。有界 → 不會
    # 無限卡住 Anki 關閉。
    _STOP_WAIT_MS = 15000
    _CLOSE_WAIT_MS = 3000    # 收尾後關窗前的等待上限（見 _force_close）

    def _active_worker(self):
        """回目前在跑的 worker，沒有就 None。
        worker 不掛在 self 上的視窗要覆寫（Batch Operations 的掛在 section 上）。"""
        w = getattr(self, "_worker", None)
        return w if (w is not None and w.isRunning()) else None

    def _batch_active(self):
        return self._active_worker() is not None

    def _request_stop(self):
        w = self._active_worker()
        if w is not None and hasattr(w, "stop"):
            w.stop()

    def _set_batch_status(self, text):
        """顯示「正在停批」訊息；沒有 status 標籤的視窗覆寫這個。"""
        status = getattr(self, "status", None)
        if status is not None:
            status.setText(text)

    def done(self, r):
        if self._batch_active():
            self._close_pending = True
            self._close_result = r
            self._request_stop()
            self._set_batch_status(
                "Stopping the batch — this window closes when the current card is done.")
            return
        aqt.dialogs.markClosed(self._DM_NAME)
        super().done(r)

    def _end_batch(self):
        """worker 收尾時由各 dialog 的 _on_finished / _on_error 呼叫：
        釋放批次權，若使用者在跑批中按過 Close 就補上真正的關窗。"""
        _batch_release(self._BATCH_LABEL)
        if getattr(self, "_close_pending", False):
            self._close_pending = False
            self._force_close(getattr(self, "_close_result", 0))

    def _force_close(self, r):
        """真的關窗。不走 done() 的守門：worker 用的是自訂 finished signal，
        在 run() 還沒返回時就發出 → 這一刻 isRunning() 仍是 True，走 done()
        會被守門擋掉、視窗永遠關不掉。收尾 signal 既然已發完，剩下的只是 thread
        退出，等一下即可（順帶避免 GC 掉還在跑的 QThread）。"""
        w = self._active_worker()
        if w is not None:
            w.wait(self._CLOSE_WAIT_MS)
        aqt.dialogs.markClosed(self._DM_NAME)
        self._close_pending = False        # 與 closeWithCallback 一致:關掉了就不留旗標
        super().done(r)

    def closeWithCallback(self, callback):
        w = self._active_worker()
        if w is not None:
            self._request_stop()
            w.wait(self._STOP_WAIT_MS)     # 有界等待:worker 真的結束才放 Anki 卸載 collection
        _batch_release(self._BATCH_LABEL)
        self._close_pending = False
        try:
            self._force_close(0)
        finally:
            callback()


def _hline():
    """Horizontal separator line between panel sections."""
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def _section_title(text):
    lbl = QLabel(f"▸ {text}")
    lbl.setStyleSheet("font-weight:700; font-size:14px; color:#1E293B; padding-top:4px;")
    return lbl


def _selectable(label):
    """文字可以用滑鼠選取、複製（QLabel 預設不行）。同 ⌘S 的單字與退回原因。"""
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def _selectable_all(widget):
    """widget 底下所有文字標籤都設成可選取。Qt 沒有全域預設可改（QLabel 預設只有連結能點），
    所以每個視窗建好、以及每次重建清單後各呼叫一次；check_qt_runtime.py 掃過每個視窗守住。"""
    for label in widget.findChildren(QLabel):
        _selectable(label)


# ── ⌘F 區塊共用的清單框與按鈕列（三個區塊長得一樣：改這裡就全部一起改）────────────
LIST_MAX_PX = 400            # 清單框最多多高，超過就捲動
BUTTON_MIN_WIDTH = 140       # 區塊動作鈕的寬度一致，上下對得齊
LIST_TEXT_STYLE = "color:#475569; padding:4px;"


LIST_RADIUS = 6              # 清單框圓角（跟輸入框、按鈕一樣圓）


class ListBox(QWidget):
    """清單框範本：沒東西就一行高（顯示一句灰字），有東西就跟著長高，最多 LIST_MAX_PX、超過捲動。
    兩種用法：set_text() 放一段文字（紅旗卡、長句）；clear_rows()＋add_row() 放自訂的列（刪重複）。
    圓角外框用 QPainter 自己畫、不用 stylesheet：父元件一套 stylesheet，底下的 QCheckBox 會改走
    stylesheet 繪製，在 Anki 裡出過「點方框沒反應」（見 CLAUDE.md）。顏色取調色盤，深色模式照樣對。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        pad = LIST_RADIUS // 2            # 內容縮進圓角裡，方形的捲動區不會蓋到圓角
        outer.setContentsMargins(pad, pad, pad, pad)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self._scroll)
        self.body = QWidget()
        self.body.setAutoFillBackground(False)
        self._rows = QVBoxLayout(self.body)
        self._rows.setContentsMargins(4, 4, 4, 4)
        self.text = _selectable(QLabel(""))
        self.text.setWordWrap(True)
        self.text.setStyleSheet(LIST_TEXT_STYLE)
        self._rows.addWidget(self.text)
        self._scroll.setWidget(self.body)
        self._fit_width = -1
        self.fit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), LIST_RADIUS, LIST_RADIUS)
        p.end()

    def set_text(self, text):
        self.clear_rows()
        self.text.setText(text)
        self.text.setVisible(True)
        self.fit()

    def clear_rows(self):
        while self._rows.count() > 1:
            w = self._rows.takeAt(1).widget()
            if w is not None:
                w.deleteLater()

    def add_row(self, widget):
        self.text.setVisible(False)
        self._rows.addWidget(widget)

    def fit(self):
        """高度跟著內容（換行的文字依目前寬度算）。加完列要呼叫一次。"""
        self._rows.activate()
        width = self._scroll.viewport().width()
        if width > 0 and self._rows.hasHeightForWidth():
            content = self._rows.heightForWidth(width)
        else:
            content = self.body.sizeHint().height()
        m = self.layout().contentsMargins()
        self.setFixedHeight(min(content + m.top() + m.bottom(), LIST_MAX_PX))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._scroll.viewport().width() != self._fit_width:   # 寬度變了 → 換行跟著變，高度重算
            self._fit_width = self._scroll.viewport().width()
            self.fit()


def _button_row(*buttons):
    """靠右、不留外邊距、每顆一樣寬的按鈕列（回 QWidget，區塊可以整列顯示/隱藏）。"""
    row_w = QWidget()
    row = QHBoxLayout(row_w)
    row.setContentsMargins(0, 0, 0, 0)
    row.addStretch()
    for b in buttons:
        b.setMinimumWidth(BUTTON_MIN_WIDTH)
        row.addWidget(b)
    return row_w


# ── 生成中的即時進度（⌘A 狀態列、⌘S 每一列）─────────────────────────────────────

def _activity_line(text, elapsed, left=None):
    """'Image · searching Pexels · 4s (6s left)'。left＝找圖的倒數（其餘步驟沒有）。"""
    line = f"{text} · {elapsed}s"
    return line if left is None else f"{line} ({max(left, 0)}s left)"


class LiveActivity:
    """記住「現在在做什麼」與開始時間，給視窗的 QTimer 每秒組成一行字。
    倒數只在找圖那一步（worker 送來 countdown>0 時開始，換到別的步驟就收掉）。"""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._t0 = clock()
        self.text = ""
        self._deadline = None

    def update(self, text, countdown=0):
        self.text = text
        if countdown > 0:
            self._deadline = self._clock() + countdown
        elif not text.startswith("Image"):
            self._deadline = None

    def line(self):
        if not self.text:
            return ""
        now = self._clock()
        left = None if self._deadline is None else math.ceil(self._deadline - now)
        return _activity_line(self.text, int(now - self._t0), left)


def _show_nonmodal(dialog_cls):
    """批次類視窗用非阻塞方式開啟（show() 而非 exec()）：不鎖 Anki 主視窗，
    生成跑很久時可以移開/縮小視窗、繼續用 Anki。
    Settings 不是批次視窗，維持 modal exec()。"""
    dlg = aqt.dialogs.open(dialog_cls._DM_NAME)   # 不查 _DM_NAMES：讓開窗函式不依賴入口檔的登記表
    dlg.show()
    return dlg
