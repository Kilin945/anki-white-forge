"""dispatcher 的路由/斷路器/failover 純邏輯測試（假 provider、假時鐘、免網路）。"""
from unittest.mock import patch

import pytest

import core.dispatcher as disp
from core.providers import ProviderError


class TestCircuitBreaker:
    def test_starts_closed(self):
        assert disp.CircuitBreaker().allows() is True

    def test_opens_after_threshold(self):
        b = disp.CircuitBreaker(threshold=3)
        with patch.object(disp.time, "monotonic", return_value=1000.0):
            for _ in range(3):
                b.record_failure()
            assert b.allows() is False
            assert b.open_remaining() == pytest.approx(30.0, abs=0.2)

    def test_below_threshold_stays_closed(self):
        b = disp.CircuitBreaker(threshold=3)
        b.record_failure()
        b.record_failure()
        assert b.allows() is True

    def test_success_resets_streak(self):
        b = disp.CircuitBreaker(threshold=3)
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.record_failure()
        b.record_failure()
        assert b.allows() is True          # 中間成功過 → 連續失敗歸零

    def test_half_open_after_cooldown_then_success_closes(self):
        b = disp.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(disp.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            clock["t"] = 1031.0
            assert b.allows() is True       # half-open：放試探
            b.record_success()
            assert b.allows() is True       # 復位

    def test_half_open_probe_failure_reopens(self):
        b = disp.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(disp.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            clock["t"] = 1031.0
            assert b.allows() is True
            b.record_failure()              # 試探失敗 → 立刻再 OPEN
            assert b.allows() is False

    def test_failure_cooldown_uses_provider_reset(self):
        b = disp.CircuitBreaker(threshold=1, cooldown_default=30.0)
        with patch.object(disp.time, "monotonic", return_value=1000.0):
            b.record_failure(cooldown=12.0)    # 冷卻=該家 reset 時間，不瞎猜
            assert b.open_remaining() == pytest.approx(12.0, abs=0.2)


class FakeProvider:
    """可編程假 provider：headroom 可為值或 callable；replies/errors 腳本化。"""
    def __init__(self, name, headroom=1.0, reset=0.0, reply="ok", error=None):
        self.name = name
        self._headroom = headroom
        self._reset = reset
        self._reply = reply
        self._error = error
        self.calls = 0

    def generate(self, prompt, **kw):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._reply

    def headroom(self):
        return self._headroom() if callable(self._headroom) else self._headroom

    def reset_secs(self):
        return self._reset


class TestRouting:
    def test_picks_higher_headroom(self):
        a = FakeProvider("groq", headroom=0.3, reply="from-groq")
        b = FakeProvider("gemini", headroom=0.9, reply="from-gemini")
        d = disp.Dispatcher([a, b])
        assert d.generate("p") == "from-gemini"
        assert (a.calls, b.calls) == (0, 1)

    def test_switches_when_headroom_shifts(self):
        state = {"h": 0.9}
        a = FakeProvider("groq", headroom=0.5, reply="from-groq")
        b = FakeProvider("gemini", headroom=lambda: state["h"], reply="from-gemini")
        d = disp.Dispatcher([a, b])
        assert d.generate("p") == "from-gemini"
        state["h"] = 0.1                      # gemini 額度被打低
        assert d.generate("p") == "from-groq"
        state["h"] = 1.0                      # 窗口 reset，額度恢復 → 路由自動回來
        assert d.generate("p") == "from-gemini"

    def test_zero_headroom_excluded(self):
        a = FakeProvider("groq", headroom=0.0)
        b = FakeProvider("gemini", headroom=0.2, reply="from-gemini")
        d = disp.Dispatcher([a, b])
        assert d.generate("p") == "from-gemini"
        assert a.calls == 0


class TestFailover:
    def test_failover_to_other_provider(self):
        a = FakeProvider("groq", headroom=0.9, error=ProviderError("boom"))
        b = FakeProvider("gemini", headroom=0.5, reply="rescued")
        d = disp.Dispatcher([a, b])
        assert d.generate("p") == "rescued"
        assert (a.calls, b.calls) == (1, 1)

    def test_all_fail_raises_limited(self):
        a = FakeProvider("groq", headroom=0.9, reset=40.0, error=ProviderError("x"))
        b = FakeProvider("gemini", headroom=0.5, reset=15.0, error=ProviderError("y"))
        d = disp.Dispatcher([a, b])
        with pytest.raises(disp.AllProvidersLimited) as ei:
            d.generate("p")
        assert ei.value.soonest_reset == pytest.approx(15.0)
        assert set(ei.value.resets) == {"groq", "gemini"}

    def test_both_exhausted_raises_limited_without_calling(self):
        a = FakeProvider("groq", headroom=0.0, reset=40.0)
        b = FakeProvider("gemini", headroom=0.0, reset=15.0)
        d = disp.Dispatcher([a, b])
        with pytest.raises(disp.AllProvidersLimited) as ei:
            d.generate("p")
        assert (a.calls, b.calls) == (0, 0)
        assert ei.value.resets["groq"] == pytest.approx(40.0)
        assert ei.value.soonest_reset == pytest.approx(15.0)


class TestBreakerIntegration:
    def test_broken_provider_skipped_until_cooldown(self):
        err = ProviderError("boom")
        a = FakeProvider("groq", headroom=0.9, error=err)
        b = FakeProvider("gemini", headroom=0.5, reply="ok")
        clock = {"t": 1000.0}
        with patch.object(disp.time, "monotonic", side_effect=lambda: clock["t"]):
            d = disp.Dispatcher([a, b], threshold=3, cooldown_default=30.0)
            for _ in range(3):
                d.generate("p")            # groq 每次先被選中、失敗、failover 到 gemini
            assert a.calls == 3            # 3 次後 groq 斷路器 OPEN
            d.generate("p")
            assert a.calls == 3            # OPEN 期間 groq 不再被打
            clock["t"] = 1031.0            # 冷卻過 → half-open 試探
            a._error = None
            a._reply = "groq-back"
            assert d.generate("p") == "groq-back"
