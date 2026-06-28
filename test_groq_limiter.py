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


# ── wall detection (no waiting — caller stops immediately when near the limit) ──
class TestWallDetection:
    def test_clear_before_any_response(self):
        assert Limiter().wall_secs() == 0.0          # quota unknown → clear to call

    def test_clear_when_plenty(self):
        lim = Limiter()
        lim.update(_headers(11963))
        assert lim.wall_secs() == 0.0                 # remaining >= floor → clear

    def test_wall_when_low(self):
        lim = Limiter()
        with patch.object(addon.time, "monotonic", return_value=1000.0):
            lim.update(_headers(500, reset="30s"))    # below floor → wall, 30s away
            assert lim.wall_secs() == pytest.approx(30.0, abs=0.2)

    def test_clear_once_reset_window_passed(self):
        lim = Limiter()
        clock = {"t": 1000.0}
        with patch.object(addon.time, "monotonic", side_effect=lambda: clock["t"]):
            lim.update(_headers(500, reset="10s"))
            clock["t"] = 1030.0                       # reset window already elapsed
            assert lim.wall_secs() == 0.0

    def test_never_sleeps(self):
        lim = Limiter()
        with patch.object(addon.time, "monotonic", return_value=1000.0), \
             patch.object(addon.time, "sleep") as sleep:
            lim.update(_headers(10, reset="50s"))
            lim.wall_secs()
            sleep.assert_not_called()                 # 限速器絕不自己等


# ── concurrency stress ─────────────────────────────────────────────────────────
class TestConcurrencyStress:
    def test_no_crash_or_deadlock_under_load(self):
        """64 執行緒 × 500 次同時 update()/wall_secs()（模擬 ⌘S 並發猛打）→ 不崩、不死鎖。"""
        lim = Limiter()
        errors = []

        def worker(wid):
            try:
                for j in range(500):
                    lim.update(_headers((wid * 37 + j * 11) % 13000, reset="20ms"))
                    lim.wall_secs()
            except Exception as e:        # noqa: BLE001 — 任何例外都算測試失敗
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(64)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"執行緒丟出例外:{errors[:3]}"
        assert all(not t.is_alive() for t in threads), "有執行緒卡住（疑似死鎖）"

    def test_stops_before_429(self):
        """一分鐘 12000 token 預算、每次 600。限速器應在『真的超量(429)』前就讓呼叫端停下。"""
        budget, per_call = 12000, 600
        used = {"v": 0, "over": 0, "stopped": False}
        lim = Limiter()
        with patch.object(addon.time, "monotonic", return_value=1000.0):
            for _ in range(40):                  # 40×600=24000 遠超預算 → 一定得中途停
                if lim.wall_secs() > 0:          # 接近上限 → 立刻停,不再打
                    used["stopped"] = True
                    break
                used["v"] += per_call
                remaining = budget - used["v"]
                if remaining < 0:
                    used["over"] += 1            # 真的超量(撞 429)
                lim.update(_headers(max(remaining, 0), reset="40s"))

        assert used["stopped"], "限速器沒讓它停下"
        assert used["over"] == 0, f"超量 {used['over']} 次 — 應該在撞牆前一步就停"
