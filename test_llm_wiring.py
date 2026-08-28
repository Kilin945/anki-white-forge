"""core/llm.py 接上 dispatcher 後的行為測試（假 dispatcher 注入、免網路）。"""
from unittest.mock import patch

import pytest

import core.llm as llm_mod
from core.dispatcher import AllProvidersLimited, Dispatcher
from core.rate_limiter import RateLimitReached


class _FakeDispatcher:
    def __init__(self, reply=None, exc=None, providers=("x",)):
        self.providers = list(providers)
        self._reply = reply
        self._exc = exc
        self.kwargs = None

    def generate(self, prompt, **kw):
        self.kwargs = kw
        if self._exc:
            raise self._exc
        return self._reply


class TestWiring:
    def test_generate_returns_text(self):
        with patch.object(llm_mod, "_dispatcher", _FakeDispatcher(reply="hi")):
            assert llm_mod.groq_generate("p") == "hi"

    def test_generate_swallows_limited(self):
        exc = AllProvidersLimited({"groq": 40.0})
        with patch.object(llm_mod, "_dispatcher", _FakeDispatcher(exc=exc)):
            assert llm_mod.groq_generate("p") == ""

    def test_strict_translates_limited_to_ratelimitreached(self):
        exc = AllProvidersLimited({"groq": 40.0, "gemini": 15.0})
        with patch.object(llm_mod, "_dispatcher", _FakeDispatcher(exc=exc)):
            with pytest.raises(RateLimitReached) as ei:
                llm_mod.groq_generate_strict("p")
            assert ei.value.retry_after == 16          # soonest(15) 無條件進位 +1

    def test_no_providers_returns_empty(self):
        with patch.object(llm_mod, "_dispatcher", _FakeDispatcher(providers=())):
            assert llm_mod.groq_generate("p") == ""
            assert llm_mod.groq_generate_strict("p") == ""

    def test_strict_uses_lower_temperature(self):
        fake = _FakeDispatcher(reply="x")
        with patch.object(llm_mod, "_dispatcher", fake):
            llm_mod.groq_generate_strict("p")
            assert fake.kwargs["temperature"] == 0.3
            llm_mod.groq_generate("p")
            assert fake.kwargs["temperature"] == 0.7

    def test_engine_description_lists_providers(self):
        class _P:
            def __init__(self, name, model):
                self.name, self.model = name, model
        fake = _FakeDispatcher()
        fake.providers = [_P("groq", "llama-3.3-70b-versatile"), _P("gemini", "gemini-2.0-flash")]
        with patch.object(llm_mod, "_dispatcher", fake):
            s = llm_mod.engine_description()
            assert "groq" in s and "gemini" in s

    def test_load_groq_client_still_importable(self):
        assert callable(llm_mod._load_groq_client)     # test_backfill.py 靠它


class TestSentenceEffort:
    """造句走較高思考等級（effort=medium）；其他呼叫維持預設 low。"""

    def test_llm_sentence_uses_medium_effort(self):
        fake = _FakeDispatcher(reply="A cat sat on the warm mat.")
        with patch.object(llm_mod, "_dispatcher", fake):
            llm_mod.llm_sentence("cat")
        assert fake.kwargs["effort"] == "medium"

    def test_llm_sentence_and_query_uses_medium_effort(self):
        fake = _FakeDispatcher(reply="A cat sat.\ncat photo")
        with patch.object(llm_mod, "_dispatcher", fake):
            llm_mod.llm_sentence_and_query("cat")
        assert fake.kwargs["effort"] == "medium"

    def test_llm_translate_defaults_to_low_effort(self):
        fake = _FakeDispatcher(reply="貓")
        with patch.object(llm_mod, "_dispatcher", fake):
            llm_mod.llm_translate("cat", "A cat sat.")
        assert fake.kwargs["effort"] == "low"
