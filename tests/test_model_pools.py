"""分池分流（addon/_llm_dispatch.py）：每模型一個 provider、依任務分池、Gemini 每日額度。"""
import importlib.util
import io
import pathlib
import urllib.error
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "addon_llm_dispatch_pools",
    pathlib.Path(__file__).parent.parent / "addon" / "_llm_dispatch.py")
lld = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lld)


class FakeProvider:
    def __init__(self, name, headroom=1.0, reset=0.0, reply="ok", error=None):
        self.name, self._headroom, self._reset = name, headroom, reset
        self._reply, self._error, self.calls = reply, error, 0

    def generate(self, prompt, **kw):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._reply

    def headroom(self):
        return self._headroom

    def reset_secs(self):
        return self._reset


def test_task_routes_to_its_own_pool():
    big, small = FakeProvider("big", reply="S"), FakeProvider("small", reply="L")
    d = lld.Dispatcher({"sentence": [big], "light": [small]})
    assert d.generate("p", temperature=0, max_tokens=8, timeout=5, task="sentence") == "S"
    assert d.generate("p", temperature=0, max_tokens=8, timeout=5, task="light") == "L"
    assert (big.calls, small.calls) == (1, 1)


def test_default_task_is_light():
    big, small = FakeProvider("big"), FakeProvider("small")
    lld.Dispatcher({"sentence": [big], "light": [small]}).generate(
        "p", temperature=0, max_tokens=8, timeout=5)
    assert (big.calls, small.calls) == (0, 1)


def test_list_means_one_pool_for_every_task():
    p = FakeProvider("only")
    d = lld.Dispatcher([p])
    d.generate("p", temperature=0, max_tokens=8, timeout=5, task="sentence")
    d.generate("p", temperature=0, max_tokens=8, timeout=5, task="light")
    assert p.calls == 2 and d.providers == [p]


def test_ties_keep_pool_order():
    a, b = FakeProvider("a"), FakeProvider("b")
    lld.Dispatcher({"light": [a, b], "sentence": []}).generate(
        "p", temperature=0, max_tokens=8, timeout=5)
    assert (a.calls, b.calls) == (1, 0)


def test_404_model_falls_over_to_next():
    dead = FakeProvider("dead", error=lld.ProviderError("HTTP 404"))
    alive = FakeProvider("alive", reply="ok")
    d = lld.Dispatcher({"light": [dead, alive], "sentence": []}, threshold=1)
    for _ in range(3):
        assert d.generate("p", temperature=0, max_tokens=8, timeout=5) == "ok"
    assert dead.calls == 1            # 斷路器打開後就不再試


def test_wall_secs_per_pool():
    s = FakeProvider("s", headroom=0.0, reset=20.0)
    l = FakeProvider("l", headroom=1.0)
    d = lld.Dispatcher({"sentence": [s], "light": [l]})
    assert d.wall_secs("light") == 0.0
    assert d.wall_secs("sentence") == pytest.approx(20.0)
    assert d.wall_secs() == pytest.approx(20.0)      # 任一池全滅就算牆


def test_all_limited_raises_with_that_pools_resets():
    s = FakeProvider("s", headroom=0.0, reset=9.0)
    d = lld.Dispatcher({"sentence": [s], "light": [FakeProvider("l")]})
    with pytest.raises(lld.AllProvidersLimited) as ei:
        d.generate("p", temperature=0, max_tokens=8, timeout=5, task="sentence")
    assert ei.value.resets == {"s": pytest.approx(9.0)}


def test_provider_names_carry_the_model():
    assert lld.GroqProvider("k", "openai/gpt-oss-20b").name == "groq:openai/gpt-oss-20b"
    assert lld.GeminiProvider("k", "gemini-3.5-flash", 5).name == "gemini:gemini-3.5-flash"


def test_lite_model_sends_no_thinking_config():
    seen = {}

    def fake_urlopen(req, timeout):
        import json
        seen.update(json.loads(req.data.decode()))
        raise urllib.error.URLError("stop")

    with patch.object(lld.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(lld.ProviderError):
            lld.GeminiProvider("k", "gemini-3.5-flash-lite", 15).generate(
                "p", temperature=0, max_tokens=32, timeout=5)
    assert "thinkingConfig" not in seen["generationConfig"]
    assert seen["generationConfig"]["maxOutputTokens"] == 32


_DAILY_429 = ('{"error": {"code": 429, "details": [{"violations": [{"quotaId": '
              '"GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}, '
              '{"retryDelay": "65152s"}]}}')


def test_gemini_daily_quota_cools_down_until_reset():
    err = urllib.error.HTTPError("u", 429, "quota", {}, io.BytesIO(_DAILY_429.encode()))
    g = lld.GeminiProvider("k", "gemini-3.5-flash", 5)
    with patch.object(lld.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(lld.ProviderRateLimited) as ei:
            g.generate("p", temperature=0, max_tokens=8, timeout=5)
    assert ei.value.retry_after == pytest.approx(65152.0)   # 不套 60 秒上限
    assert g.headroom() == 0.0
    assert g.reset_secs() > 60000


def test_gemini_per_minute_429_still_backs_off_short():
    body = '{"error": {"code": 429, "details": [{"retryDelay": "13s"}]}}'
    err = urllib.error.HTTPError("u", 429, "rpm", {}, io.BytesIO(body.encode()))
    with patch.object(lld.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(lld.ProviderRateLimited) as ei:
            lld.GeminiProvider("k", "gemini-3.5-flash", 5).generate(
                "p", temperature=0, max_tokens=8, timeout=5)
    assert ei.value.retry_after <= 60.0


def test_neuron_budget_blocks_near_daily_limit():
    with patch.object(lld.time, "time", return_value=86400 * 100 + 10):
        b = lld.NeuronBudget(daily=1000, reserve=100)
        b.record(850)
        assert b.headroom() > 0.0
        b.record(60)                                   # 剩 90 < reserve
        assert b.headroom() == 0.0
        assert b.reset_secs() == pytest.approx(86400 - 10)


def test_neuron_budget_resets_next_utc_day():
    clock = [86400 * 100 + 10]
    with patch.object(lld.time, "time", side_effect=lambda: clock[0]):
        b = lld.NeuronBudget(daily=1000, reserve=100)
        b.mark_exhausted()
        assert b.headroom() == 0.0
        clock[0] = 86400 * 101 + 1
        assert b.headroom() == 1.0


def _cf_response(text, neurons=20.0):
    import json
    body = json.dumps({"success": True, "result": {"response": text,
                                                   "usage": {"neurons": neurons}}}).encode()

    class R(io.BytesIO):
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R(body)


def test_cloudflare_generate_records_neurons():
    b = lld.NeuronBudget(daily=1000, reserve=0)
    p = lld.CloudflareProvider("acct", "tok", "@cf/openai/gpt-oss-120b", b)
    with patch.object(lld.urllib.request, "urlopen", return_value=_cf_response("Hi.", 30.0)):
        assert p.generate("p", temperature=0, max_tokens=8, timeout=5) == "Hi."
    assert p.name == "cloudflare:@cf/openai/gpt-oss-120b"
    assert b.headroom() == pytest.approx(0.97)


def test_cloudflare_choices_shape():
    import json
    body = json.dumps({"success": True, "result": {
        "choices": [{"message": {"content": "Yo."}}], "usage": {"neurons": 5}}}).encode()

    class R(io.BytesIO):
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *a): return False
    p = lld.CloudflareProvider("a", "t", "@cf/x", lld.NeuronBudget(1000, 0))
    with patch.object(lld.urllib.request, "urlopen", return_value=R(body)):
        assert p.generate("p", temperature=0, max_tokens=8, timeout=5) == "Yo."


def test_cloudflare_daily_exhausted_marks_budget():
    body = b'{"success": false, "errors": [{"code": 4006, "message": "you have used up your daily free allocation of 10,000 neurons"}]}'
    err = urllib.error.HTTPError("u", 429, "x", {}, io.BytesIO(body))
    b = lld.NeuronBudget(daily=10000, reserve=0)
    p = lld.CloudflareProvider("a", "t", "@cf/x", b)
    with patch.object(lld.urllib.request, "urlopen", side_effect=err):
        with pytest.raises(lld.ProviderRateLimited):
            p.generate("p", temperature=0, max_tokens=8, timeout=5)
    assert b.headroom() == 0.0


def test_cloudflare_missing_or_bad_key_file(tmp_path, monkeypatch):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    assert lld._load_cloudflare(str(tmp_path / "nope")) is None
    bad = tmp_path / "bad"
    bad.write_text("just garbage\n")
    assert lld._load_cloudflare(str(bad)) is None
    good = tmp_path / "good"
    good.write_text("CLOUDFLARE_ACCOUNT_ID=a1\nCLOUDFLARE_API_TOKEN=t1\n")
    assert lld._load_cloudflare(str(good)) == ("a1", "t1")


def test_build_pools_skips_families_without_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(lld, "GROQ_KEY_PATH", str(tmp_path / "g"))
    monkeypatch.setattr(lld, "GEMINI_KEY_PATH", str(tmp_path / "m"))
    monkeypatch.setattr(lld, "CLOUDFLARE_KEY_PATH", str(tmp_path / "c"))
    for v in ("GROQ_API_KEY", "GEMINI_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / "g").write_text("gk")
    pools = lld.build_pools()
    assert [p.name for p in pools["sentence"]] == ["groq:openai/gpt-oss-120b"]
    assert [p.name for p in pools["light"]] == ["groq:openai/gpt-oss-20b"]


def test_build_pools_cloudflare_models_share_one_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(lld, "GROQ_KEY_PATH", str(tmp_path / "g"))
    monkeypatch.setattr(lld, "GEMINI_KEY_PATH", str(tmp_path / "m"))
    monkeypatch.setattr(lld, "CLOUDFLARE_KEY_PATH", str(tmp_path / "c"))
    for v in ("GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    (tmp_path / "c").write_text("CLOUDFLARE_ACCOUNT_ID=a\nCLOUDFLARE_API_TOKEN=t\n")
    pools = lld.build_pools()
    cf = pools["sentence"] + pools["light"]
    assert len(cf) == 2 and cf[0]._budget is cf[1]._budget


def test_no_lite_in_sentence_pool():
    assert not [m for _, m, _ in lld.SENTENCE_POOL if "lite" in m]


def test_groq_chat_forwards_task():
    import addon

    class Rec:
        providers = ["x"]
        def generate(self, prompt, **kw):
            self.kw = kw
            return "ok"
    rec = Rec()
    with patch.object(addon._llm, "_dispatcher", rec):
        addon._llm._groq_chat("p", temperature=0, max_tokens=8, timeout=5, task="sentence")
    assert rec.kw["task"] == "sentence"


def test_sentence_call_uses_sentence_pool():
    import addon
    seen = {}

    def fake_chat(prompt, **kw):
        seen.update(kw)
        return "The penguin waddled across the ice."
    with patch.object(addon._llm, "_groq_chat", fake_chat):
        w = addon._workers.Worker.__new__(addon._workers.Worker)
        w._llm_sentence("penguin")
    assert seen["task"] == "sentence"


def test_gate_only_checks_pools_the_card_needs():
    import addon

    class D:
        def wall_secs(self, task=None):
            return {"sentence": 30.0, "light": 0.0}[task]
    bw = addon._workers.BackfillWorker.__new__(addon._workers.BackfillWorker)
    with patch.object(addon._llm, "_dispatcher", D()):
        assert bw._wall_for(need_sentence=False) == 0.0
        assert bw._wall_for(need_sentence=True) == 30.0


def test_stop_message_names_only_the_blocked_pool():
    import addon

    class D:
        def wall_secs(self, task=None):
            return {"sentence": 40.0, "light": 0.0}[task]

        def resets(self, task=None):
            return {"sentence": {"gemini:pro": 40.0, "groq:big": 55.0},
                    "light": {"groq:small": 0.0}}[task]
    bw = addon._workers.BackfillWorker.__new__(addon._workers.BackfillWorker)
    bw._hit_limit = bw._stopped = False
    bw.retry_after = 0
    bw.limit_resets = {}
    bw.limit_pools = []
    note = {"noteId": 1, "fields": {"Front": {"value": "cat"},
                                    "Sentence": {"value": ""}}}
    with patch.object(addon._llm, "_dispatcher", D()):
        assert bw._process_one(note).startswith("skip")
    assert bw.limit_pools == ["sentence"]
    assert set(bw.limit_resets) == {"gemini:pro", "groq:big"}
    assert "~40s" in addon._llm_dispatch.format_reset_summary(bw.limit_resets)
    assert bw.retry_after == 41
