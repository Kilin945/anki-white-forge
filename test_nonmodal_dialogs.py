"""批次視窗非阻塞化之後的三道防線（純邏輯測試）。

`exec()` 除了「擋住使用者」之外，還隱性提供三件事；換成非阻塞 `show()` 就得自己重建：

1. **批次互斥**——同時只能有一個批次在跑。否則兩個 worker 會同時搶速率額度、
   對同一批卡的同一欄位重複寫（⌘S 與 ⌘F 都會寫 Sentence_CN）。
2. **關窗即結束批次**——跑批中關窗不能留下還在寫卡的殭屍 worker。
3. **Anki 退出時收拾**——改用 Anki 內建 `aqt.dialogs`，退出/切 profile 時
   `closeAll` 才看得到這些視窗（自製 registry 看不到 → worker 會對正在卸載的
   collection 繼續寫）。

外加：非阻塞後「視窗開著時卡片被別處刪掉」變成可達路徑 → `_live_note` 防呆。

Qt 實際視窗行為走手動驗證；這裡測抽出來的決策邏輯。
addon 會 import Anki 的 aqt（測試環境沒有）→ 假 aqt 由 conftest.py 統一安裝。
"""
import pytest

import addon  # 假 aqt 已由 conftest.py 安裝


# ── 批次互斥 ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _free_batch():
    addon._batch_release(addon._batch_busy())
    yield
    addon._batch_release(addon._batch_busy())


class TestBatchMutex:
    def test_acquire_when_free(self):
        assert addon._batch_acquire("Complete Missing Cards") is True
        assert addon._batch_busy() == "Complete Missing Cards"

    def test_second_batch_blocked_even_from_another_window(self):
        assert addon._batch_acquire("Complete Missing Cards") is True
        # ⌘F 的整句翻譯與 ⌘S 都會寫 Sentence_CN → 不准同時跑
        assert addon._batch_acquire("Batch Operations") is False
        assert addon._batch_busy() == "Complete Missing Cards"

    def test_release_frees_for_the_next_batch(self):
        addon._batch_acquire("Complete Missing Cards")
        addon._batch_release("Complete Missing Cards")
        assert addon._batch_busy() is None
        assert addon._batch_acquire("Batch Operations") is True

    def test_release_by_non_owner_is_ignored(self):
        addon._batch_acquire("Complete Missing Cards")
        addon._batch_release("Batch Operations")      # 別的視窗收尾不該解別人的鎖
        assert addon._batch_busy() == "Complete Missing Cards"

    def test_reacquire_by_same_owner_is_allowed(self):
        assert addon._batch_acquire("Add English Word") is True
        assert addon._batch_acquire("Add English Word") is True


# ── 關窗不留殭屍 worker ───────────────────────────────────────────────────────

class _FakeWorker:
    def __init__(self, running=True):
        self._running = running
        self.stopped = 0
        self.waited = []

    def isRunning(self):
        return self._running

    def stop(self):
        self.stopped += 1

    def wait(self, msecs):
        self.waited.append(msecs)
        self._running = False
        return True


class _FakeBase:
    """假 QDialog 基底:只記錄 done() 有沒有被真的呼叫。"""
    def __init__(self):
        self.done_calls = []
        self.shown = 0

    def done(self, r):
        self.done_calls.append(r)

    def show(self):
        self.shown += 1


class _FakeDM:
    def __init__(self):
        self.closed = []

    def markClosed(self, name):
        self.closed.append(name)


class _Dlg(addon._BatchDialogMixin, _FakeBase):
    _DM_NAME = "WhiteForgeTest"
    _BATCH_LABEL = "Test Window"

    def __init__(self, worker=None):
        super().__init__()
        self._worker = worker
        self.status_msgs = []

    def _set_batch_status(self, text):
        self.status_msgs.append(text)


@pytest.fixture
def dm(monkeypatch):
    fake = _FakeDM()
    monkeypatch.setattr(addon.aqt, "dialogs", fake)
    return fake


class TestCloseWhileRunning:
    def test_idle_close_marks_closed_and_closes(self, dm):
        dlg = _Dlg(worker=None)
        dlg.done(0)
        assert dlg.done_calls == [0]
        assert dm.closed == ["WhiteForgeTest"]

    def test_close_during_batch_stops_instead_of_closing(self, dm):
        worker = _FakeWorker(running=True)
        dlg = _Dlg(worker=worker)
        dlg.done(0)
        assert dlg.done_calls == []          # 視窗不關 → worker 的 signal 不會打到死物件
        assert worker.stopped == 1
        assert dm.closed == []
        assert dlg.status_msgs                # 有告訴使用者正在停

    def test_window_closes_once_the_batch_ends(self, dm):
        worker = _FakeWorker(running=True)
        dlg = _Dlg(worker=worker)
        dlg.done(0)                           # 使用者按 Close → 只停批
        worker._running = False
        dlg._end_batch()                      # worker 收尾（各 dialog 的 _on_finished 呼叫）
        assert dlg.done_calls == [0]          # 這時才真的關窗
        assert dm.closed == ["WhiteForgeTest"]

    def test_end_batch_without_pending_close_keeps_window_open(self, dm):
        worker = _FakeWorker(running=False)
        dlg = _Dlg(worker=worker)
        dlg._end_batch()
        assert dlg.done_calls == []           # 沒人要關 → 留著看結果
        assert dm.closed == []

    def test_end_batch_releases_the_mutex(self, dm):
        dlg = _Dlg(worker=_FakeWorker(running=False))
        addon._batch_acquire(_Dlg._BATCH_LABEL)
        dlg._end_batch()
        assert addon._batch_busy() is None


class TestAnkiShutdown:
    def test_close_with_callback_waits_for_the_worker(self, dm):
        worker = _FakeWorker(running=True)
        dlg = _Dlg(worker=worker)
        called = []
        dlg.closeWithCallback(lambda: called.append(True))
        assert worker.stopped == 1
        assert worker.waited                  # 真的等到 thread 結束才放 Anki 走
        assert called == [True]
        assert dm.closed == ["WhiteForgeTest"]

    def test_close_with_callback_when_idle_returns_immediately(self, dm):
        dlg = _Dlg(worker=None)
        called = []
        dlg.closeWithCallback(lambda: called.append(True))
        assert called == [True]
        assert dm.closed == ["WhiteForgeTest"]


# ── Anki 內建 dialog manager ──────────────────────────────────────────────────

class TestDialogManagerRegistration:
    def test_all_four_batch_dialogs_are_registered(self):
        assert set(addon._DM_NAMES) == {
            addon.AddWordDialog, addon.BackfillDialog,
            addon.FindDuplicatesDialog, addon.BatchOperationsDialog,
        }

    def test_registry_names_are_unique_and_namespaced(self):
        names = list(addon._DM_NAMES.values())
        assert len(set(names)) == len(names)
        assert all(n.startswith("WhiteForge") for n in names)

    def test_show_nonmodal_delegates_to_anki_dialog_manager(self, monkeypatch):
        # Anki 的 DialogManager.open() 自帶單例、還原縮小視窗、raise、reopen() 重掃
        opened = []

        class _Opener:
            def open(self, name):
                opened.append(name)
                return _FakeBase()

        monkeypatch.setattr(addon.aqt, "dialogs", _Opener())
        addon._show_nonmodal(addon.BackfillDialog)
        assert opened == [addon._DM_NAMES[addon.BackfillDialog]]

    def test_batch_dialogs_declare_a_close_contract(self):
        # register_dialog 要求:silentlyClose 或 closeWithCallback,否則退出時收不掉
        for cls in addon._DM_NAMES:
            assert getattr(cls, "silentlyClose", False) or hasattr(cls, "closeWithCallback"), \
                f"{cls.__name__} 沒宣告 closeAll 契約"

    def test_rescanning_dialogs_expose_reopen(self):
        # 單例被叫回前面時 DialogManager 會呼叫 reopen() → 重掃,不然清單是舊的
        for cls in (addon.BackfillDialog, addon.FindDuplicatesDialog):
            assert hasattr(cls, "reopen"), f"{cls.__name__} 缺 reopen(),叫回來會是舊清單"


# ── 卡片被別處刪掉的防呆 ──────────────────────────────────────────────────────

class TestLiveNote:
    def test_returns_none_when_the_note_is_gone(self, monkeypatch):
        class _Col:
            def get_note(self, nid):
                raise Exception("NotFoundError")

        class _Mw:
            col = _Col()

        monkeypatch.setattr(addon, "mw", _Mw())
        assert addon._live_note(123) is None

    def test_returns_the_note_when_it_exists(self, monkeypatch):
        sentinel = object()

        class _Col:
            def get_note(self, nid):
                return sentinel

        class _Mw:
            col = _Col()

        monkeypatch.setattr(addon, "mw", _Mw())
        assert addon._live_note(123) is sentinel
