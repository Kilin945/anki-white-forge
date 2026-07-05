# test_groq_limiter.py
"""壓力測試:addon 端 HeaderLimiter(addon/_llm_dispatch.py)。

原本測 addon/__init__.py 的 _GroqLimiter;Phase 2 該邏輯搬進自足子模組
_llm_dispatch.py(升級為 headroom 介面) → 直接檔案載入測,不再需要假 aqt stub。
"""
import importlib.util
import pathlib
import threading
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "addon_llm_dispatch",
    pathlib.Path(__file__).parent / "addon" / "_llm_dispatch.py")
lld = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lld)


def _headers(remaining_tokens, reset="50ms"):
    return {"x-ratelimit-remaining-tokens": str(remaining_tokens),
            "x-ratelimit-reset-tokens": reset}


class TestParsers:
    @pytest.mark.parametrize("s, expected", [
        ("1m26.4s", 86.4), ("185ms", 0.185), ("2.5s", 2.5),
        ("1h2m", 3720.0), ("", 0.0), (None, 0.0), ("garbage", 0.0),
    ])
    def test_parse_reset_secs(self, s, expected):
        assert lld._parse_reset_secs(s) == pytest.approx(expected)

    def test_parse_int(self):
        assert lld._parse_int("999") == 999
        assert lld._parse_int(None) is None
        assert lld._parse_int("nope") is None


class TestWallDetection:
    """牆偵測語意沿用:headroom()==0 即「該停」,reset_secs() 即等待秒數。"""

    def test_clear_before_any_response(self):
        assert lld.HeaderLimiter().headroom() == 1.0     # 額度未知 → 可以打

    def test_clear_when_plenty(self):
        lim = lld.HeaderLimiter()
        lim.update(_headers(11963))
        assert lim.headroom() > 0.0

    def test_wall_when_low(self):
        lim = lld.HeaderLimiter()
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            lim.update(_headers(500, reset="30s"))
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(30.0, abs=0.2)

    def test_clear_once_reset_window_passed(self):
        lim = lld.HeaderLimiter()
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            lim.update(_headers(500, reset="10s"))
            clock["t"] = 1030.0
            assert lim.headroom() == 1.0

    def test_never_sleeps(self):
        lim = lld.HeaderLimiter()
        with patch.object(lld.time, "monotonic", return_value=1000.0), \
             patch.object(lld.time, "sleep") as sleep:
            lim.update(_headers(10, reset="50s"))
            lim.headroom()
            lim.reset_secs()
            sleep.assert_not_called()                 # 限速器絕不自己等


class TestConcurrencyStress:
    def test_no_crash_or_deadlock_under_load(self):
        """64 執行緒 × 500 次同時 update()/headroom()(模擬 ⌘S 並發猛打)→ 不崩、不死鎖。"""
        lim = lld.HeaderLimiter()
        errors = []

        def worker(wid):
            try:
                for j in range(500):
                    lim.update(_headers((wid * 37 + j * 11) % 13000, reset="20ms"))
                    lim.headroom()
            except Exception as e:        # noqa: BLE001 — 任何例外都算測試失敗
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(64)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"執行緒丟出例外:{errors[:3]}"
        assert all(not t.is_alive() for t in threads), "有執行緒卡住(疑似死鎖)"

    def test_dispatcher_stops_before_429(self):
        """headroom 見底時 wall_secs()>0,呼叫端(⌘S)在真撞 429 前就停。"""
        class _P:
            name = "groq"
            def __init__(self, lim):
                self._lim = lim
            def headroom(self):
                return self._lim.headroom()
            def reset_secs(self):
                return self._lim.reset_secs()
            def generate(self, prompt, **kw):
                return "ok"

        lim = lld.HeaderLimiter()
        d = lld.Dispatcher([_P(lim)])
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            assert d.wall_secs() == 0.0
            lim.update(_headers(500, reset="40s"))    # 低於 floor
            assert d.wall_secs() == pytest.approx(40.0, abs=0.2)
