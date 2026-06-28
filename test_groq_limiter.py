"""壓力測試:Groq 自適應限速器 _GroqLimiter（addon/__init__.py）。

addon 模組會 import Anki 的 aqt，測試環境沒有 → 先用一個「萬用」假模組頂替 aqt，
就能在 Anki 外載入 addon、直接測真正的限速器程式碼（不必搬程式、不必碰 Anki）。
"""
import sys
import types
import threading
from unittest.mock import patch

import pytest


# ── 用萬用 stub 頂替 aqt，讓 addon 能被 import ──────────────────────────────────
class _AnyMeta(type):
    def __getattr__(cls, _):
        return _Any()


class _Any(metaclass=_AnyMeta):
    """什麼都能做的替身:可被繼承、可呼叫、任何屬性存取都回另一個替身。"""
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
import addon  # noqa: E402  (the real addon module, limiter included)

Limiter = addon._GroqLimiter


def _headers(remaining_tokens, reset="50ms"):
    return {"x-ratelimit-remaining-tokens": str(remaining_tokens),
            "x-ratelimit-reset-tokens": reset}


# ── header parser ──────────────────────────────────────────────────────────────
class TestParsers:
    @pytest.mark.parametrize("s, expected", [
        ("1m26.4s", 86.4), ("185ms", 0.185), ("2.5s", 2.5),
        ("1h2m", 3720.0), ("", 0.0), (None, 0.0), ("garbage", 0.0),
    ])
    def test_parse_reset_secs(self, s, expected):
        assert addon._parse_reset_secs(s) == pytest.approx(expected)

    def test_parse_int(self):
        assert addon._parse_int("999") == 999
        assert addon._parse_int(None) is None
        assert addon._parse_int("nope") is None


# ── pacing logic ─────────────────────────────────────────────────────────────
class TestPacing:
    def test_no_wait_before_any_response(self):
        lim = Limiter()
        with patch.object(addon.time, "sleep") as sleep:
            lim.before()
            sleep.assert_not_called()        # quota unknown → never block

    def test_no_wait_when_plenty(self):
        lim = Limiter()
        lim.update(_headers(11963))
        with patch.object(addon.time, "sleep") as sleep:
            lim.before()
            sleep.assert_not_called()        # remaining >= floor → go straight through

    def test_waits_when_low(self):
        lim = Limiter()
        with patch.object(addon.time, "monotonic", return_value=1000.0):
            lim.update(_headers(500, reset="30s"))   # below floor, reset 30s away
            with patch.object(addon.time, "sleep") as sleep:
                lim.before()
                sleep.assert_called_once()
                assert sleep.call_args[0][0] == pytest.approx(30.1, abs=0.2)

    def test_skips_overlong_wait(self):
        lim = Limiter()
        with patch.object(addon.time, "monotonic", return_value=1000.0):
            lim.update(_headers(500, reset="200s"))   # > _MAX_WAIT(65) → don't block forever
            with patch.object(addon.time, "sleep") as sleep:
                lim.before()
                sleep.assert_not_called()

    def test_clears_remaining_after_waiting(self):
        lim = Limiter()
        with patch.object(addon.time, "monotonic", return_value=1000.0):
            lim.update(_headers(500, reset="10s"))
            with patch.object(addon.time, "sleep"):
                lim.before()
        with patch.object(addon.time, "sleep") as sleep:   # post-wait: assume refilled
            lim.before()
            sleep.assert_not_called()


# ── concurrency stress ─────────────────────────────────────────────────────────
class TestConcurrencyStress:
    def test_no_crash_or_deadlock_under_load(self):
        """64 執行緒 × 200 次同時 update()/before()（模擬 ⌘S 並發猛打）→ 不崩、不死鎖。"""
        lim = Limiter()
        errors = []

        def worker(wid):
            try:
                for j in range(200):
                    lim.update(_headers((wid * 37 + j * 11) % 13000, reset="20ms"))
                    lim.before()
            except Exception as e:        # noqa: BLE001 — 任何例外都算測試失敗
                errors.append(e)

        with patch.object(addon.time, "sleep"):   # 不真的睡，純壓邏輯/鎖
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(64)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

        assert not errors, f"執行緒丟出例外:{errors[:3]}"
        assert all(not t.is_alive() for t in threads), "有執行緒卡住（疑似死鎖）"

    def test_burst_stays_under_budget(self):
        """模擬一分鐘 12000 token 預算、並發補卡。限速器應讓總用量不超過預算（不撞 429）。"""
        budget = 12000
        per_call = 600
        state = {"used": 0, "over": 0}        # over = 模擬撞 429 的次數
        glock = threading.Lock()
        lim = Limiter()

        # 假時鐘:睡覺 = 視窗重置、預算回滿
        clock = {"t": 1000.0}

        def fake_sleep(secs):
            with glock:
                state["used"] = 0             # 視窗重置
            clock["t"] += secs

        def fake_monotonic():
            return clock["t"]

        def worker():
            for _ in range(20):
                lim.before()
                with glock:
                    state["used"] += per_call
                    remaining = budget - state["used"]
                    if remaining < 0:
                        state["over"] += 1    # 沒擋住 → 撞牆
                        remaining = 0
                lim.update(_headers(remaining, reset="40s"))

        with patch.object(addon.time, "sleep", side_effect=fake_sleep), \
             patch.object(addon.time, "monotonic", side_effect=fake_monotonic):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

        # 8×20=160 次呼叫、每次 600 token，遠超 12000 → 沒限速器一定爆；有限速器應該 0 次撞牆
        assert state["over"] == 0, f"撞牆 {state['over']} 次，限速器沒擋住"
