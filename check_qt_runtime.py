#!/usr/bin/env python3
"""Anki / Qt 升級後的 addon **執行期**檢查（姊妹檔:check_qt_compat.py）。

分工:
  check_qt_compat.py   建得起來嗎 —— import、Qt 名稱、四個對話框建構、快捷鍵綁定、
                       aqt.dialogs 的登記 / 單例 / closeAll
  check_qt_runtime.py  跑得起來嗎 —— 本檔。QThread 跑完整批、pyqtSignal 跨執行緒送達、
                       批次互斥、跑批中關窗的三條路徑、QMessageBox 自訂 ButtonRole、
                       QTest 真實按鍵、64-bit note id 經 signal 無損

兩支都載入 **Anki.app 內實際在用的那份** PyQt6 與 aqt(`Contents/Resources/app_packages`
加進 sys.path;只能加這個,`Resources/app` 下另一個 anki 套件會遮蔽真正的),offscreen
執行,不需要 Anki 開著。

所有對外呼叫(LLM / TTS / 圖片 / subprocess / AnkiConnect 的 urllib)全部被換成 stub,
寫入只進到記憶體裡的假 collection。**不會碰到正在執行的 Anki,也不會動到真卡片。**

    uv run python check_qt_runtime.py
    ANKI_APP=/path/to/Anki.app uv run python check_qt_runtime.py

覆蓋面只到「想得到的破法」為止。之後每遇到一種升級踩雷,就往這裡加一條。
"""
import json, logging, os, pathlib, sys, tempfile, threading, time, types, io

APP = pathlib.Path(os.environ.get("ANKI_APP", "/Applications/Anki.app"))
PKGS = APP / "Contents/Resources/app_packages"
REPO = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(PKGS))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# never touch the real rotating log file
logging.getLogger("whiteforge.llm").addHandler(logging.NullHandler())

from PyQt6.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, Qt, QTimer, QEventLoop
from PyQt6.QtWidgets import QApplication, QMenu, QWidget, QMessageBox, QPushButton
from PyQt6.QtTest import QTest
import aqt

_app = QApplication.instance() or QApplication([])
MAIN_TID = threading.get_ident()

_passed, _failed = 0, []
def check(label):
    def deco(fn):
        global _passed
        try:
            detail = fn()
            _passed += 1
            print(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
        except Exception as e:
            import traceback; tb = traceback.format_exc().strip().splitlines()[-3:]
            _failed.append(label)
            print(f"  FAIL  {label} — {type(e).__name__}: {e}")
            for l in tb: print(f"        {l}")
    return deco

# ── fake collection ──────────────────────────────────────────────────────────
NID_BASE = 1758000000123          # 13 digits > 2**31 → exercises 64-bit ids

class FakeNote(dict):
    def __init__(self, nid, **f):
        super().__init__(f); self.id = nid; self.tags = []

FIELDS = ["Front","Association","Sentence","Sentence_CN","Image_Prompt",
          "Audio","Front_Audio","Translation"]

class FakeCol:
    def __init__(self):
        self._notes = {}
        self.calls = {"update_note":0,"add_note":0,"remove_notes":0,"flag":0,"save":0}
        self.models = types.SimpleNamespace(by_name=lambda n: {"id":1})
        self.decks  = types.SimpleNamespace(id=lambda n: 1)
        self.media  = types.SimpleNamespace(dir=lambda: MEDIA_DIR)
        self._next = NID_BASE + 900
        self.flagged = set()
    def add(self, nid, **f):
        base = {k:"" for k in FIELDS}; base.update(f)
        self._notes[nid] = FakeNote(nid, **base); return self._notes[nid]
    def find_notes(self, q):
        if q.startswith("tag:"):
            t = q.split(":",1)[1]
            return [n.id for n in self._notes.values() if t in n.tags]
        if 'Front:"' in q:
            w = q.split('Front:"',1)[1].rstrip('"')
            return [n.id for n in self._notes.values() if n["Front"] == w]
        return list(self._notes)
    def find_cards(self, q):
        if "flag:1" in q:
            return [nid*10 for nid in self.flagged if nid in self._notes]
        return []
    def get_card(self, cid): return types.SimpleNamespace(nid=cid//10)
    def get_note(self, nid):
        if nid not in self._notes: raise KeyError(f"note {nid} gone")
        return self._notes[nid]
    def new_note(self, model):
        self._next += 1
        return self.add(self._next)
    def add_note(self, note, did): self.calls["add_note"] += 1
    def update_note(self, note):   self.calls["update_note"] += 1
    def remove_notes(self, nids):
        self.calls["remove_notes"] += 1
        for n in nids: self._notes.pop(n, None)
    def set_user_flag_for_cards(self, flag, cids): self.calls["flag"] += 1
    def save(self): self.calls["save"] += 1

class FakeAddonManager:
    def getConfig(self, _n): return None
    def writeConfig(self, _n, _c): pass

class FakeMw(QWidget):
    def __init__(self):
        super().__init__()
        self.form = types.SimpleNamespace(menuTools=QMenu("Tools", self))
        self.addonManager = FakeAddonManager()
        self.col = FakeCol()
        self.resets = 0
    def reset(self): self.resets += 1

# 假 col.media.dir() 用的暫存目錄 — 不要建在 repo 裡
MEDIA_DIR = tempfile.mkdtemp(prefix="whiteforge-qtcheck-")

mw = FakeMw()
aqt.mw = mw
sys.path.insert(0, str(REPO))
import addon

# ── stub every outbound call ─────────────────────────────────────────────────
GATE = threading.Event(); GATE.set()      # held closed to keep a batch running
WORKER_TIDS = set()
HTTP_CALLS = []

def stub_sentence(self, word, association=""):
    WORKER_TIDS.add(threading.get_ident())
    GATE.wait(timeout=30)
    return (f"The engineer used {word} in a short stub sentence.", "Stub")

addon.Worker._llm_sentence            = stub_sentence
addon.Worker._groq_sentence           = lambda self,w,a="": f"stub {w}"
addon.Worker._groq_translate          = lambda self,w,s: "字義"
addon.Worker._groq_translate_sentence = lambda self,s,strict=False: "這是中文翻譯。"
addon.Worker._fetch_image             = lambda self,w,definition="",sentence="": "<img src='stub.jpg'>"
addon.Worker._make_audio_batch        = lambda self,items: None
addon._groq_spellcheck                = lambda w: ("ok", None)
addon.subprocess = types.SimpleNamespace(
    run=lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr=""),
    TimeoutExpired=Exception)

class _FakeResp:
    def __init__(self, body): self._b = body
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False

def _fake_urlopen(req, timeout=None):
    """Stands in for AnkiConnect: records the payload and applies it to the fake
    collection, so the post-run _note_incomplete recount is realistic."""
    body = json.loads(req.data.decode())
    HTTP_CALLS.append(body)
    if body.get("action") == "updateNoteFields":
        n = body["params"]["note"]
        note = mw.col._notes.get(n["id"])
        if note is None:      # 真 AnkiConnect 對已刪除的 note 會回 error
            return _FakeResp(json.dumps({"result": None,
                                         "error": "note was not found: %d" % n["id"]}).encode())
        for k, v in n["fields"].items():
            note[k] = v
    return _FakeResp(json.dumps({"result": None, "error": None}).encode())

class _FakeReq:
    def __init__(self, url, data=None, headers=None): self.url=url; self.data=data
addon.urllib = types.SimpleNamespace(
    request=types.SimpleNamespace(urlopen=_fake_urlopen, Request=_FakeReq),
    error=types.SimpleNamespace(HTTPError=Exception, URLError=Exception))

WARNINGS = []
addon.showWarning = lambda m, *a, **k: WARNINGS.append(m)
addon.tooltip     = lambda *a, **k: None

# ── helpers ──────────────────────────────────────────────────────────────────
def pump(cond, timeout=25.0, label=""):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        if cond(): return True
        time.sleep(0.01)
    raise TimeoutError(f"timeout waiting for {label or cond}")

NAMES = ["probealpha","probebravo","probecharlie","probedelta","probeecho",
         "probefoxtrot","probegolf","probehotel"]


def seed(n=3, flagged=0, longsent=0):
    mw.col._notes.clear(); mw.col.flagged.clear()
    for k in mw.col.calls: mw.col.calls[k] = 0
    for i in range(n):
        mw.col.add(NID_BASE + i, Front=NAMES[i], Association="hint")
    for i in range(flagged):
        nid = NID_BASE + 500 + i
        mw.col.add(nid, Front="flaggedone" + "x"*i, Sentence="done", Audio="[sound:x]",
                   Front_Audio="[sound:y]", Image_Prompt="<img src=a>",
                   Translation="義", Sentence_CN="中")
        mw.col.flagged.add(nid)
    for i in range(longsent):
        mw.col.add(NID_BASE + 600 + i, Front="longone" + "x"*i,
                   Sentence=" ".join(["word"]*40), Audio="[sound:x]",
                   Front_Audio="[sound:y]", Image_Prompt="<img src=a>",
                   Translation="義", Sentence_CN="中")
    HTTP_CALLS.clear()

def open_backfill():
    dlg = addon.BackfillDialog(mw); dlg.show(); _app.processEvents(); return dlg

def select_all_rows(dlg):
    for r in dlg._rows.values(): r.checkbox.setChecked(True)
    _app.processEvents()

print(f"Anki.app : {APP}   aqt {aqt.appVersion}")
print(f"PyQt     : {PYQT_VERSION_STR}   Qt {QT_VERSION_STR}   Python {sys.version.split()[0]}")
print()

# ═══ 1. QThread full batch round + cross-thread signals + 64-bit ids ═════════
print("1) QThread 真的跑完一輪 ⌘S 批次")

STEP_TIDS, DONE_TIDS, FIN_TIDS = set(), set(), set()
SEEN_IDS, DONE_IDS = [], []
FIN_RUNNING = []

@check("⌘S: 3 張卡跑完整批次（worker 起→處理→finished→_end_batch）")
def _():
    seed(3)
    dlg = open_backfill()
    assert len(dlg._rows) == 3, f"scan 只看到 {len(dlg._rows)} 列"
    select_all_rows(dlg)
    assert dlg.run_btn.isEnabled(), "Complete Selected 沒被啟用"
    orig_step, orig_done, orig_fin = dlg._on_step, dlg._on_card_done, dlg._on_finished
    def s(nid, f, st):
        STEP_TIDS.add(threading.get_ident()); SEEN_IDS.append(nid); orig_step(nid, f, st)
    def d(nid):
        DONE_TIDS.add(threading.get_ident()); DONE_IDS.append(nid); orig_done(nid)
    def fin(res):
        FIN_TIDS.add(threading.get_ident())
        FIN_RUNNING.append(dlg._worker.isRunning())
        orig_fin(res)
    dlg._on_step, dlg._on_card_done, dlg._on_finished = s, d, fin
    QTest.mouseClick(dlg.run_btn, Qt.MouseButton.LeftButton)   # 真的按鈕點擊
    pump(lambda: bool(FIN_TIDS), 30, "batch finished")
    globals()["DLG1"] = dlg
    return f"status: {dlg.status.text()!r}"

@check("⌘S: 收尾狀態 = 全部完成、批次權已釋放、Remove Selected 出現")
def _():
    dlg = DLG1
    assert dlg.status.text().startswith("Done — 3 card(s) completed"), dlg.status.text()
    assert addon._batch_busy() is None, f"批次權沒釋放：{addon._batch_busy()}"
    assert dlg.remove_btn.isVisible(), "Remove Selected 沒出現"
    assert not dlg.progress_bar.isVisible(), "進度條沒收起來"
    assert mw.col.calls["save"] >= 1 and mw.resets >= 1, "沒有 col.save()/mw.reset()"
    return f"save={mw.col.calls['save']} reset={mw.resets}"

@check("⌘S: 3 張卡各發出一次 AnkiConnect updateNoteFields（本輪被攔截，未真寫）")
def _():
    acts = [c["action"] for c in HTTP_CALLS]
    assert acts == ["updateNoteFields"]*3, acts
    ids = sorted(c["params"]["note"]["id"] for c in HTTP_CALLS)
    assert ids == [NID_BASE, NID_BASE+1, NID_BASE+2], ids
    flds = HTTP_CALLS[0]["params"]["note"]["fields"]
    for k in ("Sentence","Audio","Front_Audio","Image_Prompt","Translation","Sentence_CN"):
        assert k in flds, f"{k} 沒被寫"
    return f"每張寫 {len(flds)} 欄"

@check("pyqtSignal 跨執行緒：worker 在別的 thread，slot 全部落在 GUI thread")
def _():
    assert WORKER_TIDS, "worker 沒跑到"
    assert MAIN_TID not in WORKER_TIDS, "worker 竟然在 GUI thread 跑"
    assert STEP_TIDS == {MAIN_TID}, f"step slot 在 {STEP_TIDS}"
    assert DONE_TIDS == {MAIN_TID}, f"card_done slot 在 {DONE_TIDS}"
    assert FIN_TIDS  == {MAIN_TID}, f"finished slot 在 {FIN_TIDS}"
    return f"worker 用了 {len(WORKER_TIDS)} 條 thread，slot 全在 main"

@check("64-bit note id 經 signal 傳遞無損（13 位數、非 32-bit 可容）")
def _():
    assert NID_BASE > 2**31, NID_BASE
    assert set(DONE_IDS) == {NID_BASE, NID_BASE+1, NID_BASE+2}, DONE_IDS
    assert all(isinstance(i, int) for i in SEEN_IDS)
    assert set(SEEN_IDS) <= {NID_BASE, NID_BASE+1, NID_BASE+2}, set(SEEN_IDS)
    return f"{len(SEEN_IDS)} 次 step + {len(DONE_IDS)} 次 card_done，id 全數相符"

print(f"  INFO  finished 送達時 worker.isRunning() = {FIN_RUNNING}"
      "  （競態，取決於 main loop 何時取件；_force_close 的必要性改在第 3 組驗）")

DLG1.close(); _app.processEvents()

# ═══ 2. 批次互斥 ════════════════════════════════════════════════════════════
print("\n2) 批次互斥（_batch_acquire / _blocked_by_batch）")

def seed_rich():
    seed(3, flagged=2, longsent=2)
    for i in range(2):                       # 有句無翻譯 → TranslateSection 掃得到
        mw.col.add(NID_BASE + 700 + i, Front="cnmissing" + "x"*i,
                   Sentence="This card already has a full English sentence.",
                   Audio="[sound:x]", Front_Audio="[sound:y]",
                   Image_Prompt="<img src=a>", Translation="義", Sentence_CN="")

def start_held_batch():
    """開一輪 ⌘S 並用 GATE 卡住，回傳 dialog（批次維持在跑）。"""
    GATE.clear()
    dlg = open_backfill()
    select_all_rows(dlg)
    QTest.mouseClick(dlg.run_btn, Qt.MouseButton.LeftButton)
    pump(lambda: addon._batch_busy() is not None, 10, "batch acquired")
    pump(lambda: len(WORKER_TIDS) > 0, 10, "worker running")
    return dlg

@check("批次跑起來後 _batch_acquire 對別的 label 回 False、對持有者回 True")
def _():
    seed_rich()
    globals()["DLG2"] = start_held_batch()
    assert addon._batch_busy() == "Complete Missing Cards", addon._batch_busy()
    assert addon._batch_acquire("Add English Word") is False
    assert addon._batch_acquire("Batch Operations") is False
    assert addon._batch_acquire("Complete Missing Cards") is True, "持有者自己被擋"
    addon._batch_release("Add English Word")          # 非持有者不能解鎖
    assert addon._batch_busy() == "Complete Missing Cards", "被非持有者解鎖了"
    return "訊息：" + addon._batch_busy_message()

@check("⌘D Find Duplicates 的 Delete 在跑批中被擋（卡片沒被刪）")
def _():
    before = mw.col.calls["remove_notes"]
    d = addon.FindDuplicatesDialog(mw); d.show(); _app.processEvents()
    d._on_delete()
    assert "batch is already running" in d.status.text(), d.status.text()
    assert mw.col.calls["remove_notes"] == before, "竟然刪了卡"
    d.close(); _app.processEvents()
    return d.status.text()

@check("⌘F 四個 section 的動作在跑批中全被擋（沒有任何寫入）")
def _():
    snap = dict(mw.col.calls)
    panel = addon.BatchOperationsDialog(mw); panel.show(); _app.processEvents()
    tr, flag, long_, test = panel._sections
    assert tr._notes, "TranslateSection 沒掃到缺翻譯的卡，擋不擋無從測"
    assert flag._flagged, "ClearFlaggedSection 沒掃到紅旗卡"
    assert long_._hits, "LongSentencesSection 沒掃到長句"
    tr._start(60)
    assert "batch is already running" in tr.status.text(), tr.status.text()
    assert tr._worker is None or not tr._worker.isRunning(), "第二個 worker 起來了"
    flag._on_clear()
    assert "batch is already running" in flag.status.text(), flag.status.text()
    long_._on_clear()
    assert "batch is already running" in long_.status.text(), long_.status.text()
    test._on_add()
    assert "batch is already running" in test.status.text(), test.status.text()
    test._on_clean()
    assert "batch is already running" in test.status.text(), test.status.text()
    assert dict(mw.col.calls) == snap, f"有寫入發生：{snap} -> {mw.col.calls}"
    panel.close(); _app.processEvents()
    return "translate / clear-flagged / long-sentences / test-cards 五個入口全擋下"

@check("⌘A Add 在跑批中被擋（不會起第二個 Worker）")
def _():
    a = addon.AddWordDialog(mw); a.show(); _app.processEvents()
    a.word_input.setText("mutexprobe")
    a._on_add()
    assert "batch is already running" in a.status.text(), a.status.text()
    assert a._worker is None, "竟然起了第二個 Worker"
    assert mw.col.calls["add_note"] == 0, "竟然建了卡"
    a.close(); _app.processEvents()
    return a.status.text()

# ═══ 3. 跑批中關窗：延後關閉 / _force_close / closeWithCallback ═════════════
print("\n3) 跑批中關窗的三條路徑")

BF_NAME = addon.BackfillDialog._DM_NAME

@check("跑批中按 Close：視窗不關，只請 worker 停（done() 的守門）")
def _():
    dlg = DLG2
    assert dlg.isVisible() and addon._batch_busy() is not None
    dlg.done(0)                                    # = Close 鈕 / X / Esc 的共同漏斗
    _app.processEvents()
    assert dlg.isVisible(), "視窗直接關掉了 → worker 的 signal 會打到死物件"
    assert dlg._close_pending is True
    assert dlg._worker._stopped is True, "沒有請 worker 停"
    assert dlg.status.text().startswith("Stopping the batch"), dlg.status.text()
    return dlg.status.text()

@check("worker 收尾後視窗才真的關，批次權釋放")
def _():
    dlg = DLG2
    GATE.set()                                     # 放行，讓 worker 收尾
    pump(lambda: not dlg.isVisible(), 30, "deferred close")
    assert addon._batch_busy() is None, "批次權沒釋放"
    assert dlg._close_pending is False
    return "關窗發生在 worker 收尾之後"

@check("Esc 鍵（QTest 真按鍵）走同一個 done() 漏斗 → 沒跑批時直接關")
def _():
    seed(2)
    dlg = addon._show_nonmodal(addon.BackfillDialog)
    _app.processEvents()
    assert aqt.dialogs._dialogs[BF_NAME][1] is dlg, "aqt.dialogs 沒登記這個實例"
    QTest.keyClick(dlg, Qt.Key.Key_Escape)
    _app.processEvents()
    assert not dlg.isVisible(), "Esc 沒關掉視窗"
    assert aqt.dialogs._dialogs[BF_NAME][1] is None, "Esc 關窗沒經過 markClosed"
    return "Esc → reject → done() → markClosed → 關窗"

@check("_force_close：worker 還在跑也關得掉（done() 在此刻會被自己擋住）")
def _():
    seed_rich()
    GATE.clear()
    dlg = addon._show_nonmodal(addon.BackfillDialog)
    select_all_rows(dlg)
    QTest.mouseClick(dlg.run_btn, Qt.MouseButton.LeftButton)
    pump(lambda: addon._batch_busy() is not None, 10, "batch acquired")
    assert dlg._active_worker() is not None
    dlg.done(0); _app.processEvents()
    assert dlg.isVisible(), "done() 沒擋住"            # 證明只有 _force_close 關得掉
    t0 = time.monotonic()
    dlg._force_close(0)
    waited = time.monotonic() - t0
    assert not dlg.isVisible(), "_force_close 也關不掉"
    assert aqt.dialogs._dialogs[BF_NAME][1] is None, "沒 markClosed"
    assert waited <= dlg._CLOSE_WAIT_MS/1000 + 1.5, f"等了 {waited:.1f}s，超過上限"
    GATE.set()
    pump(lambda: addon._batch_busy() is None, 30, "batch released")
    return f"worker 仍在跑仍關得掉；等待 {waited:.1f}s（上限 {dlg._CLOSE_WAIT_MS/1000}s）"

@check("closeWithCallback（Anki 退出 / 切 profile）：停批、等 thread 真的結束、才回呼")
def _():
    seed_rich()
    GATE.clear()
    dlg = addon._show_nonmodal(addon.BackfillDialog)
    select_all_rows(dlg)
    QTest.mouseClick(dlg.run_btn, Qt.MouseButton.LeftButton)
    pump(lambda: addon._batch_busy() is not None, 10, "batch acquired")
    called = []
    threading.Timer(0.4, GATE.set).start()          # 模擬手上那張卡做完
    t0 = time.monotonic()
    dlg.closeWithCallback(lambda: called.append(1))
    waited = time.monotonic() - t0
    assert called == [1], "callback 沒被呼叫 → Anki 會卡在退出流程"
    assert not dlg.isVisible()
    assert dlg._worker.isRunning() is False, "thread 還活著就放 Anki 卸載 collection"
    assert addon._batch_busy() is None
    assert aqt.dialogs._dialogs[BF_NAME][1] is None
    return f"等 {waited:.1f}s（上限 {dlg._STOP_WAIT_MS/1000}s）後才放行"

@check("aqt.dialogs.closeAll() 在跑批中收得乾淨（真正的退出路徑）")
def _():
    seed_rich()
    GATE.clear()
    dlg = addon._show_nonmodal(addon.BackfillDialog)
    addon._show_nonmodal(addon.BatchOperationsDialog)
    addon._show_nonmodal(addon.FindDuplicatesDialog)
    select_all_rows(dlg)
    QTest.mouseClick(dlg.run_btn, Qt.MouseButton.LeftButton)
    pump(lambda: addon._batch_busy() is not None, 10, "batch acquired")
    threading.Timer(0.4, GATE.set).start()
    done = []
    aqt.dialogs.closeAll(lambda: done.append(True))
    pump(lambda: bool(done), 25, "closeAll callback")
    left = [n for n in addon._DM_NAMES.values()
            if aqt.dialogs._dialogs.get(n, [None, None])[1] is not None]
    assert not left, f"沒收乾淨：{left}"
    assert addon._batch_busy() is None, "批次權沒釋放"
    assert not dlg._worker.isRunning(), "worker 還活著"
    return "三個視窗（其中一個正在跑批）全部收掉"

# ═══ 4. QMessageBox：三個自訂 ButtonRole + clickedButton() ═══════════════════
print("\n4) QMessageBox 自訂按鈕角色")

def click_modal_button(match, timeout_ms=5000):
    """等 modal QMessageBox 冒出來，按下文字含 match 的按鈕。回傳 [按到的文字]。"""
    hit = []
    t = QTimer()
    t.setInterval(30)
    state = {"ms": 0}
    def tick():
        state["ms"] += 30
        w = _app.activeModalWidget()
        if isinstance(w, QMessageBox):
            for b in w.buttons():
                if match in b.text().replace("&", ""):
                    hit.append(b.text()); t.stop(); b.click(); return
        if state["ms"] >= timeout_ms:
            t.stop()
            if w is not None: w.close()
    t.timeout.connect(tick)
    t.start()
    return hit

@check("拼字建議對話框：AcceptRole 的 Use '<建議>' → 回傳建議字")
def _():
    addon.AddWordDialog._spellcheck = lambda self, w: ("typo", "ephemeral")
    dlg = addon.AddWordDialog(mw); dlg.show(); _app.processEvents()
    hit = click_modal_button("Use '")
    out = dlg._validate_word_ui("ephemerel")
    assert hit, "沒抓到 modal QMessageBox"
    assert out == "ephemeral", out
    globals()["DLG4"] = dlg
    return f"按下 {hit[0]!r} → {out!r}"

@check("拼字建議對話框：DestructiveRole 的 Keep '<原字>' → 保留原字")
def _():
    hit = click_modal_button("Keep '")
    out = DLG4._validate_word_ui("ephemerel")
    assert out == "ephemerel", out
    return f"按下 {hit[0]!r} → {out!r}"

@check("拼字建議對話框：RejectRole 的 Cancel → None（中止新增）")
def _():
    hit = click_modal_button("Cancel")
    out = DLG4._validate_word_ui("ephemerel")
    assert out is None, out
    return f"按下 {hit[0]!r} → {out!r}（clickedButton() 三個角色都分得出來）"

@check("找不到的字：QMessageBox.question 標準 Yes/No 仍可用")
def _():
    addon.AddWordDialog._spellcheck = lambda self, w: ("nonword", None)
    hit = click_modal_button("Yes")
    out = DLG4._validate_word_ui("zzqqxx")
    assert out == "zzqqxx", out
    hit2 = click_modal_button("No")
    out2 = DLG4._validate_word_ui("zzqqxx")
    assert out2 is None, out2
    DLG4.close(); _app.processEvents()
    return f"Yes→{out!r}, No→{out2!r}"

# ═══ 5. QTest 真實按鍵 → 各自的 handler ═════════════════════════════════════
print("\n5) QTest 送真實按鍵")

@check("QLineEdit 逐字按鍵輸入（keyClicks）")
def _():
    dlg = addon.AddWordDialog(mw); dlg.show(); _app.processEvents()
    dlg.word_input.setFocus()
    QTest.keyClicks(dlg.word_input, "serendipity")
    assert dlg.word_input.text() == "serendipity", dlg.word_input.text()
    globals()["DLG5"] = dlg
    return "serendipity"

@check("Space 鍵切換勾選框 → 觸發 stateChanged → 按鈕文字跟著變")
def _():
    seed(2)
    bf = addon.BackfillDialog(mw); bf.show(); _app.processEvents()
    row = list(bf._rows.values())[0]
    assert bf.run_btn.text() == "Complete Selected (0)", bf.run_btn.text()
    QTest.keyClick(row.checkbox, Qt.Key.Key_Space)
    _app.processEvents()
    assert row.is_checked(), "Space 沒勾起來"
    assert bf.run_btn.text() == "Complete Selected (1)", bf.run_btn.text()
    QTest.keyClick(bf.select_all, Qt.Key.Key_Space)
    _app.processEvents()
    assert bf.run_btn.text() == "Complete Selected (2)", bf.run_btn.text()
    bf.close(); _app.processEvents()
    return "單列 Space → (1)，Select all Space → (2)"

@check("四組快捷鍵（Ctrl+A/S/D/F）真按下去會觸發各自的 handler")
def _():
    fired = []
    for key, act in addon.ACTIONS.items():
        act.triggered.connect(lambda _=False, k=key: fired.append(k))
        mw.addAction(act)                 # 測試環境沒有真的 menubar → 掛到 mw 上收鍵
    mw.show(); _app.processEvents()
    QTest.qWaitForWindowExposed(mw, 2000)
    keymap = {"add": Qt.Key.Key_A, "complete": Qt.Key.Key_S,
              "find_duplicates": Qt.Key.Key_D, "backfill_cn": Qt.Key.Key_F}
    opened = {}
    for name, k in keymap.items():
        QTest.keyClick(mw, k, Qt.KeyboardModifier.ControlModifier)
        _app.processEvents()
        opened[name] = [n for n in addon._DM_NAMES.values()
                        if aqt.dialogs._dialogs.get(n, [None, None])[1] is not None]
        # handler 會真的開一個非阻塞視窗並搶走 active window → 收掉再按下一個
        aqt.dialogs.closeAll(lambda: None)
        _app.processEvents()
        mw.activateWindow(); _app.processEvents()
    missing = [n for n in keymap if n not in fired]
    assert not missing, f"沒被觸發：{missing}（收到 {fired}）"
    want = {"add": "WhiteForgeAddWord", "complete": "WhiteForgeBackfill",
            "find_duplicates": "WhiteForgeDuplicates", "backfill_cn": "WhiteForgeBatchOps"}
    for n, w in want.items():
        assert opened[n] == [w], f"{n} 開出來的是 {opened[n]}，預期 {w}"
    return "Ctrl+A/S/D/F 各自開出正確視窗：" + ", ".join(
        f"{n}->{opened[n][0]}" for n in keymap)

# ═══ 6. ⌘A 完整一輪（Enter → 預設鈕 → Worker → 寫卡） ═══════════════════════
print("\n6) ⌘A Add English Word 完整一輪")

@check("Enter 觸發預設鈕 → Worker 跑完 → 卡片被建立、欄位齊全")
def _():
    seed(0)
    addon.AddWordDialog._spellcheck = lambda self, w: ("ok", None)
    GATE.set()
    dlg = addon.AddWordDialog(mw); dlg.show(); _app.processEvents()
    dlg.word_input.setText("probenovel")
    dlg.assoc_input.setText("brand new")
    fin = []
    orig = dlg._on_finished
    dlg._on_finished = lambda d: (fin.append(threading.get_ident()), orig(d))
    QTest.keyClick(dlg, Qt.Key.Key_Return)          # 預設鈕 = Add
    pump(lambda: bool(fin), 30, "add finished")
    assert fin == [MAIN_TID], "finished slot 不在 GUI thread"
    assert mw.col.calls["add_note"] == 1, mw.col.calls
    note = list(mw.col._notes.values())[-1]
    assert note["Front"] == "probenovel", note["Front"]
    assert note["Association"] == "brand new"
    assert note["Sentence"] and note["Translation"] and note["Sentence_CN"]
    assert note["Audio"] == "[sound:probenovel_tts.mp3]", note["Audio"]
    assert note["Front_Audio"] == "[sound:probenovel_word.mp3]"
    assert "<img" in note["Image_Prompt"]
    assert dlg.status.text() == "'probenovel' added!", dlg.status.text()
    assert addon._batch_busy() is None, "批次權沒釋放"
    assert dlg.word_input.text() == "" and dlg.assoc_input.text() == ""
    globals()["DLG6"] = dlg
    return "8 個欄位全部寫入 + 輸入框清空 + 批次權釋放"

@check("⌘A 重複字偵測：同一個字再加一次被擋（不會建第二張）")
def _():
    dlg = DLG6
    dlg.word_input.setText("probenovel")
    QTest.keyClick(dlg, Qt.Key.Key_Return)
    _app.processEvents()
    assert "already exists" in dlg.status.text(), dlg.status.text()
    assert mw.col.calls["add_note"] == 1, "又建了一張"
    dlg.close(); _app.processEvents()
    return dlg.status.text()

# ═══ 7. 非阻塞視窗特有的兩個路徑 ═══════════════════════════════════════════
print("\n7) 非阻塞才會出現的路徑（reopen 重掃 / 卡片被別處刪掉）")

@check("reopen()：單例被叫回前面會重掃（⌘F 清完 → 一鍵跳 ⌘S 看得到新清單）")
def _():
    seed(2)
    dlg = addon._show_nonmodal(addon.BackfillDialog)
    assert len(dlg._rows) == 2, len(dlg._rows)
    mw.col.add(NID_BASE + 300, Front="freshcard", Association="")   # 期間多了一張缺料的卡
    again = addon._show_nonmodal(addon.BackfillDialog)
    assert again is dlg, "不是單例 → 會同時開兩個批次"
    assert len(dlg._rows) == 3, f"reopen 沒重掃，仍是 {len(dlg._rows)} 列"
    dlg.close(); _app.processEvents()
    return "2 列 → reopen → 3 列"

@check("_live_note：清單還在、卡片已被別處刪掉 → 跳過而不是炸掉")
def _():
    seed(2)
    dlg = addon._show_nonmodal(addon.BackfillDialog)
    select_all_rows(dlg)
    mw.col.remove_notes(list(mw.col._notes))          # 模擬在 Browse 裡刪掉
    QTest.mouseClick(dlg.run_btn, Qt.MouseButton.LeftButton)
    _app.processEvents()
    assert "no longer exist" in dlg.status.text(), dlg.status.text()
    assert addon._batch_busy() is None, "卡片沒了卻沒把批次權還回去"
    assert dlg._worker is None or not dlg._worker.isRunning()
    dlg.close(); _app.processEvents()
    return dlg.status.text()

@check("批次跑到一半卡片被刪：worker 不會拋到 Anki，收尾計數也不算它還缺")
def _():
    seed(3)
    GATE.clear()
    dlg = addon._show_nonmodal(addon.BackfillDialog)
    select_all_rows(dlg)
    QTest.mouseClick(dlg.run_btn, Qt.MouseButton.LeftButton)
    pump(lambda: len(WORKER_TIDS) > 0, 10, "worker running")
    victim = NID_BASE + 2
    mw.col._notes.pop(victim, None)                   # 跑批中被刪 → AnkiConnect 回 error
    GATE.set()
    pump(lambda: addon._batch_busy() is None, 30, "finished")
    txt = dlg.status.text()
    assert "still need filling" not in txt, txt     # 被刪的卡不該被算成「還缺」
    assert len(mw.col._notes) == 2
    dlg.close(); _app.processEvents()
    return txt

# ═══ 8. ⌘F Batch Operations 四個 section 真的動起來 ═════════════════════════
print("\n8) ⌘F Batch Operations 四個 section 的實際動作")

def fresh_panel():
    p = addon.BatchOperationsDialog(mw); p.show(); _app.processEvents()
    return (p, *p._sections)

@check("TranslateSection：SentenceCNWorker 整輪跑完（第二種 QThread）")
def _():
    seed(0)
    for i in range(4):
        mw.col.add(NID_BASE + 800 + i, Front="cnneed" + "x"*i,
                   Sentence="This sentence still needs a Chinese translation.",
                   Audio="[sound:x]", Front_Audio="[sound:y]",
                   Image_Prompt="<img src=a>", Translation="義", Sentence_CN="")
    panel, tr, *_ = fresh_panel()
    assert len(tr._notes) == 4, len(tr._notes)
    tids = set()
    orig = tr._on_progress
    tr._on_progress = lambda d, t, r: (tids.add(threading.get_ident()), orig(d, t, r))
    run_btn = [b for b, s in tr._mode_btns if s is None][0]     # "Run to completion"
    QTest.mouseClick(run_btn, Qt.MouseButton.LeftButton)
    pump(lambda: addon._batch_busy() is None, 30, "translate batch done")
    assert tids == {MAIN_TID}, f"progress slot 在 {tids}"
    assert all(n["Sentence_CN"] for n in mw.col._notes.values()), "有卡沒被翻到"
    assert "Translated 4 this run, 0 left" in tr.status.text(), tr.status.text()
    assert len(tr._notes) == 0, "收尾沒重掃"
    panel.close(); _app.processEvents()
    return tr.status.text()

@check("TranslateSection：Stop 真的中止（剩下的沒被翻）")
def _():
    seed(0)
    for i in range(8):
        mw.col.add(NID_BASE + 820 + i, Front="cnstop" + "x"*i,
                   Sentence="Another sentence waiting for its translation.",
                   Audio="[sound:x]", Front_Audio="[sound:y]",
                   Image_Prompt="<img src=a>", Translation="義", Sentence_CN="")
    slow = threading.Event()
    def slow_tr(self, s, strict=False):
        slow.wait(timeout=10); return "慢慢翻的中文。"
    saved = addon.Worker._groq_translate_sentence
    addon.Worker._groq_translate_sentence = slow_tr
    try:
        panel, tr, *_ = fresh_panel()
        run_btn = [b for b, s in tr._mode_btns if s is None][0]
        QTest.mouseClick(run_btn, Qt.MouseButton.LeftButton)
        pump(lambda: addon._batch_busy() is not None, 10, "acquired")
        QTest.mouseClick(tr.stop_btn, Qt.MouseButton.LeftButton)   # 真的按 Stop
        assert tr.status.text() == "Stopping…", tr.status.text()
        slow.set()
        pump(lambda: addon._batch_busy() is None, 20, "stopped")
        filled = sum(1 for n in mw.col._notes.values() if n["Sentence_CN"])
        assert filled < 8, f"Stop 沒作用，8 張全翻完了"
        assert "left. Remember to sync" in tr.status.text(), tr.status.text()
        panel.close(); _app.processEvents()
        return f"按下 Stop 後只翻了 {filled}/8 → {tr.status.text()}"
    finally:
        addon.Worker._groq_translate_sentence = saved

@check("ClearFlaggedSection：清 6 欄 + 拔旗，Front / Association 保留")
def _():
    seed(0, flagged=2)
    panel, tr, flag, long_, test = fresh_panel()
    assert len(flag._flagged) == 2, flag._flagged
    assert flag.clear_btn.text() == "Clear 2 Cards", flag.clear_btn.text()
    QTest.mouseClick(flag.clear_btn, Qt.MouseButton.LeftButton)
    _app.processEvents()
    for n in mw.col._notes.values():
        assert n["Front"], "Front 被清掉了"
        for f in addon.REFILL_CLEAR_FIELDS:
            assert n[f] == "", f"{f} 沒被清"
    assert mw.col.calls["flag"] == 2, mw.col.calls
    assert mw.col.calls["update_note"] == 2
    assert "Cleared 2 card(s)" in flag.status.text(), flag.status.text()
    assert flag.post_row_w.isVisible(), "Open Complete Missing Cards 沒出現"
    panel.close(); _app.processEvents()
    return flag.status.text()

@check("LongSentencesSection：門檻掃描 + 清 5 欄，Front_Audio 保留")
def _():
    seed(0, longsent=2)
    panel, tr, flag, long_, test = fresh_panel()
    assert len(long_._hits) == 2, long_._hits
    assert long_._hits[0]["count"] == 40, long_._hits[0]
    long_.threshold_input.setText("50"); QTest.mouseClick(
        [w for w in long_.findChildren(QPushButton) if w.text() == "Rescan"][0],
        Qt.MouseButton.LeftButton)
    _app.processEvents()
    assert long_._hits == [], "門檻 50 還掃到 40 字的句子"
    long_.threshold_input.setText("20"); long_._scan(); _app.processEvents()
    assert len(long_._hits) == 2
    QTest.mouseClick(long_.clear_btn, Qt.MouseButton.LeftButton)
    _app.processEvents()
    for n in mw.col._notes.values():
        for f in addon.REBUILD_CLEAR_FIELDS:
            assert n[f] == "", f"{f} 沒被清"
        assert n["Front_Audio"] == "[sound:y]", "Front_Audio 被清掉了（單字音與句子無關）"
        assert n["Front"]
    assert "Cleared 2 card(s)" in long_.status.text(), long_.status.text()
    assert mw.col.calls["flag"] == 0, "不該動旗標"
    panel.close(); _app.processEvents()
    return long_.status.text()

@check("TestCardsSection：Add 建裸卡（打 tag）、Clean 依 tag 全刪")
def _():
    seed(0)
    panel, tr, flag, long_, test = fresh_panel()
    test.count_input.setText("3")
    QTest.mouseClick(test.add_btn, Qt.MouseButton.LeftButton); _app.processEvents()
    assert mw.col.calls["add_note"] == 3, mw.col.calls
    made = [n for n in mw.col._notes.values() if addon.TEST_CARD_TAG in n.tags]
    assert len(made) == 3 and all(n["Front"].startswith("zztest") for n in made)
    assert all(not n["Sentence"] for n in made), "測試卡不該帶句子"
    QTest.mouseClick(test.add_btn, Qt.MouseButton.LeftButton); _app.processEvents()
    assert mw.col.calls["add_note"] == 3, "重複 Add 竟然又建了一次"
    QTest.mouseClick(test.clean_btn, Qt.MouseButton.LeftButton); _app.processEvents()
    assert not [n for n in mw.col._notes.values() if addon.TEST_CARD_TAG in n.tags]
    assert "Deleted 3 test card(s)" in test.status.text(), test.status.text()
    panel.close(); _app.processEvents()
    return test.status.text()

# ── 收尾 ────────────────────────────────────────────────────────────────────
aqt.dialogs.closeAll(lambda: None)
_app.processEvents()
print()
if _failed:
    print(f"FAILED  {_passed} passed, {len(_failed)} failed")
    for f in _failed: print(f"   - {f}")
    sys.exit(1)
print(f"ALL PASS  {_passed} checks")
