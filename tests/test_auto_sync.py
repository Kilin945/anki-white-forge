"""批次寫完卡片後的自動同步：什麼情況下該同步（addon `_auto_sync_allowed`）。

真的有沒有在批次收尾後觸發同步，由 check_qt_runtime.py 用真 Qt 事件迴圈驗。
"""
import addon


def allowed(**overrides):
    state = dict(logged_in=True, media_syncing=False, progress_busy=False, batch_owner=None)
    state.update(overrides)
    return addon._batch._auto_sync_allowed(**state)


def test_syncs_when_idle_and_logged_in():
    assert allowed()


def test_skips_when_not_logged_in():
    # 自動動作不該跳出 AnkiWeb 登入框
    assert not allowed(logged_in=False)


def test_skips_while_media_still_syncing():
    # 這時按同步鍵，Anki 會開媒體同步紀錄視窗，不是同步
    assert not allowed(media_syncing=True)


def test_skips_while_a_progress_window_is_open():
    assert not allowed(progress_busy=True)


def test_skips_while_another_batch_runs():
    # 它收尾時會再觸發一次同步
    assert not allowed(batch_owner="Complete Missing Cards")
