"""core/providers.py 的 limiter 純邏輯測試（免網路、假時鐘）。"""
from unittest.mock import patch

import pytest

import core.providers as prov


def _headers(remaining, limit=12000, reset="30s"):
    return {"x-ratelimit-remaining-tokens": str(remaining),
            "x-ratelimit-limit-tokens": str(limit),
            "x-ratelimit-reset-tokens": reset}


class TestHeaderLimiter:
    def test_unknown_quota_is_full(self):
        assert prov.HeaderLimiter().headroom() == 1.0     # 還沒打過 → 視為充足

    def test_headroom_is_fraction_of_limit(self):
        lim = prov.HeaderLimiter()
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            lim.update(_headers(6000, limit=12000))
            assert lim.headroom() == pytest.approx(0.5)

    def test_below_floor_is_zero(self):
        lim = prov.HeaderLimiter(token_floor=1500)
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            lim.update(_headers(1000))
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(30.0, abs=0.2)

    def test_window_passed_refills(self):
        lim = prov.HeaderLimiter()
        clock = {"t": 1000.0}
        with patch.object(prov.time, "monotonic", side_effect=lambda: clock["t"]):
            lim.update(_headers(1000, reset="10s"))
            clock["t"] = 1020.0
            assert lim.headroom() == 1.0
            assert lim.reset_secs() == 0.0

    def test_mark_exhausted(self):
        lim = prov.HeaderLimiter()
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            lim.mark_exhausted(45)
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(45.0, abs=0.2)


class TestLocalBucketLimiter:
    def test_fresh_bucket_full(self):
        assert prov.LocalBucketLimiter(15).headroom() == 1.0

    def test_consumption_decreases(self):
        lim = prov.LocalBucketLimiter(15)
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            for _ in range(3):
                lim.record_call()
            assert lim.headroom() == pytest.approx(12 / 15)

    def test_exhausted_is_zero_with_reset(self):
        lim = prov.LocalBucketLimiter(15, window_secs=60.0)
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            for _ in range(15):
                lim.record_call()
            assert lim.headroom() == 0.0
            assert 0.0 < lim.reset_secs() <= 60.0

    def test_window_passes_refills(self):
        lim = prov.LocalBucketLimiter(15, window_secs=60.0)
        clock = {"t": 1000.0}
        with patch.object(prov.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(15):
                lim.record_call()
            clock["t"] = 1061.0
            assert lim.headroom() == 1.0

    def test_mark_exhausted(self):
        lim = prov.LocalBucketLimiter(15)
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            lim.mark_exhausted(20)
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(20.0, abs=0.2)


class TestGeminiParsing:
    def test_extract_text(self):
        data = {"candidates": [{"content": {"parts": [{"text": " A cat sat. "}]}}]}
        assert prov._extract_gemini_text(data) == "A cat sat."

    def test_extract_empty_on_bad_shape(self):
        assert prov._extract_gemini_text({}) == ""
        assert prov._extract_gemini_text({"candidates": []}) == ""

    def test_retry_secs_from_429_body(self):
        body = '{"error": {"details": [{"retryDelay": "13s"}]}}'
        assert prov._gemini_retry_secs(body) == pytest.approx(13.0)

    def test_retry_secs_default(self):
        assert prov._gemini_retry_secs("junk") == pytest.approx(30.0)


class TestGeminiBadJson:
    def test_200_with_invalid_json_raises_provider_error(self, monkeypatch):
        prov_obj = prov.GeminiProvider("fake-key")

        class _StubResponse:
            status_code = 200
            text = ""

            def json(self):
                raise ValueError("no JSON object could be decoded")

        monkeypatch.setattr(prov.requests, "post", lambda *a, **kw: _StubResponse())
        with pytest.raises(prov.ProviderError):
            prov_obj.generate("hello")


class TestBackoff:
    def test_first_429_respects_retry_after(self):
        assert prov._backoff_secs(2.0, 1) == pytest.approx(2.0)
        assert prov._backoff_secs(44.0, 1) == pytest.approx(44.0)   # 較大的 retry-after 照用

    def test_consecutive_429_escalates(self):
        assert prov._backoff_secs(2.0, 2) == pytest.approx(4.0)
        assert prov._backoff_secs(2.0, 3) == pytest.approx(8.0)
        assert prov._backoff_secs(2.0, 5) == pytest.approx(32.0)

    def test_capped_at_60(self):
        assert prov._backoff_secs(2.0, 10) == pytest.approx(60.0)
        assert prov._backoff_secs(120.0, 1) == pytest.approx(60.0)


class TestProviderLoad:
    def test_gemini_no_key_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prov, "GEMINI_KEY_PATH", str(tmp_path / "nope"))
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert prov.GeminiProvider.load() is None

    def test_gemini_key_file_loads(self, tmp_path, monkeypatch):
        p = tmp_path / ".gemini_key"
        p.write_text("AIzaFAKE\n")
        monkeypatch.setattr(prov, "GEMINI_KEY_PATH", str(p))
        g = prov.GeminiProvider.load()
        assert g is not None and g.name == "gemini"
