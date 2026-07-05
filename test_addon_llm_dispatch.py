"""addon/_llm_dispatch.py（addon 端 LLM 分流鏡像）測試。

子模組自足(stdlib-only、無 aqt) → 直接從檔案載入,不需要假 aqt stub。
鏡像 core 測試面(test_providers/test_dispatcher),外加 addon 特有的
timeout 傳遞、wall_secs()、format_reset_summary()。假時鐘、免網路。
"""
import importlib.util
import pathlib
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "addon_llm_dispatch",
    pathlib.Path(__file__).parent / "addon" / "_llm_dispatch.py")
lld = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lld)


def _headers(remaining, limit=12000, reset="30s"):
    return {"x-ratelimit-remaining-tokens": str(remaining),
            "x-ratelimit-limit-tokens": str(limit),
            "x-ratelimit-reset-tokens": reset}


class FakeProvider:
    """可編程假 provider(同 core 測試的形狀,多收 timeout)。"""
    def __init__(self, name, headroom=1.0, reset=0.0, reply="ok", error=None):
        self.name = name
        self._headroom = headroom
        self._reset = reset
        self._reply = reply
        self._error = error
        self.calls = 0
        self.last_kwargs = None

    def generate(self, prompt, **kw):
        self.calls += 1
        self.last_kwargs = kw
        if self._error is not None:
            raise self._error
        return self._reply

    def headroom(self):
        return self._headroom() if callable(self._headroom) else self._headroom

    def reset_secs(self):
        return self._reset


class TestHeaderLimiter:
    def test_unknown_quota_is_full(self):
        assert lld.HeaderLimiter().headroom() == 1.0

    def test_headroom_is_fraction_and_floor(self):
        lim = lld.HeaderLimiter(token_floor=1500)
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            lim.update(_headers(6000))
            assert lim.headroom() == pytest.approx(0.5)
            lim.update(_headers(1000))
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(30.0, abs=0.2)

    def test_window_passed_refills(self):
        lim = lld.HeaderLimiter()
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            lim.update(_headers(1000, reset="10s"))
            clock["t"] = 1020.0
            assert lim.headroom() == 1.0

    def test_mark_exhausted(self):
        lim = lld.HeaderLimiter()
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            lim.mark_exhausted(45)
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(45.0, abs=0.2)


class TestLocalBucketLimiter:
    def test_consume_exhaust_refill(self):
        lim = lld.LocalBucketLimiter(15, window_secs=60.0)
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            assert lim.headroom() == 1.0
            for _ in range(15):
                lim.record_call()
            assert lim.headroom() == 0.0
            assert 0.0 < lim.reset_secs() <= 60.0
            clock["t"] = 1061.0
            assert lim.headroom() == 1.0

    def test_mark_exhausted(self):
        lim = lld.LocalBucketLimiter(15)
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            lim.mark_exhausted(20)
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(20.0, abs=0.2)


class TestCircuitBreaker:
    def test_opens_after_threshold_and_probes(self):
        b = lld.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            assert b.allows() is False
            clock["t"] = 1031.0
            assert b.allows() is True          # half-open:放一筆
            assert b.allows() is False         # 單筆試探閘
            b.record_success()
            assert b.allows() is True          # 復位

    def test_probe_failure_reopens(self):
        b = lld.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            clock["t"] = 1031.0
            assert b.allows() is True
            b.record_failure()
            assert b.allows() is False

    def test_would_allow_does_not_consume_probe(self):
        b = lld.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            clock["t"] = 1031.0
            assert b.would_allow() is True     # 窺看
            assert b.would_allow() is True     # 再窺看,沒消費
            assert b.allows() is True          # 真的取試探
            assert b.would_allow() is False    # 試探在途 → 窺看也 False
            b.record_success()

    def test_failure_cooldown_uses_retry_after(self):
        b = lld.CircuitBreaker(threshold=1, cooldown_default=30.0)
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            b.record_failure(cooldown=12.0)
            assert b.open_remaining() == pytest.approx(12.0, abs=0.2)


class TestBackoff:
    def test_first_429_respects_retry_after(self):
        assert lld._backoff_secs(2.0, 1) == pytest.approx(2.0)
        assert lld._backoff_secs(44.0, 1) == pytest.approx(44.0)   # 較大的 retry-after 照用

    def test_consecutive_429_escalates(self):
        assert lld._backoff_secs(2.0, 2) == pytest.approx(4.0)
        assert lld._backoff_secs(2.0, 3) == pytest.approx(8.0)
        assert lld._backoff_secs(2.0, 5) == pytest.approx(32.0)

    def test_capped_at_60(self):
        assert lld._backoff_secs(2.0, 10) == pytest.approx(60.0)
        assert lld._backoff_secs(120.0, 1) == pytest.approx(60.0)


class TestDispatcher:
    def test_picks_higher_headroom_and_passes_timeout(self):
        a = FakeProvider("groq", headroom=0.3, reply="from-groq")
        b = FakeProvider("gemini", headroom=0.9, reply="from-gemini")
        d = lld.Dispatcher([a, b])
        assert d.generate("p", temperature=0.5, max_tokens=64, timeout=9) == "from-gemini"
        assert b.last_kwargs == {"temperature": 0.5, "max_tokens": 64, "timeout": 9}
        assert a.calls == 0

    def test_switches_when_headroom_shifts(self):
        state = {"h": 0.9}
        a = FakeProvider("groq", headroom=0.5, reply="from-groq")
        b = FakeProvider("gemini", headroom=lambda: state["h"], reply="from-gemini")
        d = lld.Dispatcher([a, b])
        assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "from-gemini"
        state["h"] = 0.1
        assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "from-groq"
        state["h"] = 1.0
        assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "from-gemini"

    def test_failover_and_all_limited(self):
        a = FakeProvider("groq", headroom=0.9, reset=40.0, error=lld.ProviderError("x"))
        b = FakeProvider("gemini", headroom=0.5, reply="rescued")
        d = lld.Dispatcher([a, b])
        assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "rescued"
        b._error = lld.ProviderError("y")
        b._reset = 15.0
        with pytest.raises(lld.AllProvidersLimited) as ei:
            d.generate("p", temperature=0, max_tokens=8, timeout=8)
        assert ei.value.soonest_reset == pytest.approx(15.0)
        assert set(ei.value.resets) == {"groq", "gemini"}

    def test_unexpected_exception_recorded_and_reraised(self):
        boom = FakeProvider("groq", headroom=0.9, error=ValueError("bug"))
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            d = lld.Dispatcher([boom], threshold=1, cooldown_default=30.0)
            with pytest.raises(ValueError):
                d.generate("p", temperature=0, max_tokens=8, timeout=8)
            clock["t"] = 1031.0
            boom._error = None
            boom._reply = "ok"
            assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "ok"


class TestWallSecs:
    def test_zero_when_any_provider_available(self):
        a = FakeProvider("groq", headroom=0.0, reset=40.0)
        b = FakeProvider("gemini", headroom=0.6)
        assert lld.Dispatcher([a, b]).wall_secs() == 0.0

    def test_soonest_reset_when_all_walled(self):
        a = FakeProvider("groq", headroom=0.0, reset=40.0)
        b = FakeProvider("gemini", headroom=0.0, reset=15.0)
        assert lld.Dispatcher([a, b]).wall_secs() == pytest.approx(15.0)

    def test_does_not_consume_half_open_probe(self):
        p = FakeProvider("groq", headroom=0.9, error=lld.ProviderError("x"))
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            d = lld.Dispatcher([p], threshold=1, cooldown_default=30.0)
            with pytest.raises(lld.AllProvidersLimited):
                d.generate("p", temperature=0, max_tokens=8, timeout=8)
            clock["t"] = 1031.0                 # half-open
            assert d.wall_secs() == 0.0          # 預檢看得到「可試探」
            assert d.wall_secs() == 0.0          # 且不消費試探閘
            p._error = None
            p._reply = "back"                    # 試探仍可用
            assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "back"


class TestParsersAndHelpers:
    @pytest.mark.parametrize("s, expected", [
        ("1m26.4s", 86.4), ("185ms", 0.185), ("2.5s", 2.5),
        ("1h2m", 3720.0), ("", 0.0), (None, 0.0), ("garbage", 0.0),
    ])
    def test_parse_reset_secs(self, s, expected):
        assert lld._parse_reset_secs(s) == pytest.approx(expected)

    def test_parse_int(self):
        assert lld._parse_int("999") == 999
        assert lld._parse_int(None) is None

    def test_extract_gemini_text(self):
        data = {"candidates": [{"content": {"parts": [{"text": " hi "}]}}]}
        assert lld._extract_gemini_text(data) == "hi"
        assert lld._extract_gemini_text({}) == ""

    def test_gemini_retry_secs(self):
        assert lld._gemini_retry_secs('{"error":{"details":[{"retryDelay":"13s"}]}}') == pytest.approx(13.0)
        assert lld._gemini_retry_secs("junk") == pytest.approx(30.0)

    def test_format_reset_summary(self):
        s = lld.format_reset_summary({"groq": 40.2, "gemini": 15.7})
        assert "Groq" in s and "Gemini" in s
        assert "~40s" in s and "~15s" in s

    def test_load_without_key_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lld, "GEMINI_KEY_PATH", str(tmp_path / "nope"))
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert lld.GeminiProvider.load() is None
        monkeypatch.setattr(lld, "GROQ_KEY_PATH", str(tmp_path / "nope2"))
        monkeypatch.delenv("GROQ_API_KEY", raising=False)
        assert lld.GroqProvider.load() is None

    def test_get_logger_returns_named_logger(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lld, "LOG_PATH", str(tmp_path / "addon_llm.log"))
        logger = lld.get_logger()
        assert logger.name == "whiteforge.llm"

    def test_get_logger_idempotent_no_duplicate_handlers(self, tmp_path, monkeypatch):
        # 冪等性才是重點:logger 名稱在整個測試套件內是全域的(logging 模組的登記表用
        # 名稱當 key,不分是哪個 spec_from_file_location 載入的模組實例)——所以這裡不
        # 假設呼叫前 handler 數是 0,只驗證「呼叫兩次不會疊出第二個 handler」。
        monkeypatch.setattr(lld, "LOG_PATH", str(tmp_path / "addon_llm2.log"))
        logger1 = lld.get_logger()
        count = len(logger1.handlers)
        logger2 = lld.get_logger()
        assert logger2 is logger1
        assert len(logger2.handlers) == count

    def test_load_falls_back_to_env_var(self, tmp_path, monkeypatch):
        # 與 core 同步: key 檔缺失時退 env var (檔案優先、env 其次)
        monkeypatch.setattr(lld, "GEMINI_KEY_PATH", str(tmp_path / "nope"))
        monkeypatch.setenv("GEMINI_API_KEY", "env-key")
        g = lld.GeminiProvider.load()
        assert g is not None and g.name == "gemini"
        monkeypatch.setattr(lld, "GROQ_KEY_PATH", str(tmp_path / "nope2"))
        monkeypatch.setenv("GROQ_API_KEY", "env-key2")
        assert lld.GroqProvider.load() is not None

    def test_get_logger_survives_fs_failure(self, monkeypatch):
        # 設定失敗絕不往外拋 — logging 不能炸掉 addon 載入
        lg = lld.logging.getLogger("whiteforge.llm")
        saved = lg.handlers[:]
        lg.handlers.clear()
        try:
            monkeypatch.setattr(lld.os, "makedirs",
                                lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
            out = lld.get_logger()          # 不可 raise
            assert out is lg
            assert len(lg.handlers) == 1    # NullHandler 佔位,維持冪等
        finally:
            lg.handlers.clear()
            lg.handlers.extend(saved)
