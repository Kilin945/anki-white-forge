"""整句翻譯驗證的術語白名單：多字術語保留英文不算「廢話」。core／addon 兩份 KEEP-IN-SYNC。"""
import json

import pytest

import core.llm as llm_mod
import addon


KEEP = [
    "我現在正處理 null pointer exception。",          # 事故樣本（dealing with，2026-10-02）
    "我現在正在處理 Null Pointer Exception。",         # 大小寫不同也算
    "這個 race condition 只在高負載時出現。",
    "請先開 pull request 再合併。",
    "Spring 用 dependency injection 組裝物件。",       # 單字名稱 + 白名單術語
]
REJECT = [
    "Here is the translation: 我現在正處理空指標例外。",   # 英文前言
    "Sure, the sentence means 我現在很忙。",
    "I'm dealing with a null pointer exception now.",      # 沒翻、純英文
    "",
]


class TestCore:
    def test_keeps_whitelisted_terms(self):
        for t in KEEP:
            assert llm_mod._looks_like_chinese_translation(t), t

    def test_still_rejects_preamble(self):
        for t in REJECT:
            assert not llm_mod._looks_like_chinese_translation(t), t


class TestAddon:
    def test_keeps_whitelisted_terms(self):
        for t in KEEP:
            assert addon._looks_like_chinese_translation(t), t

    def test_still_rejects_preamble(self):
        for t in REJECT:
            assert not addon._looks_like_chinese_translation(t), t


def test_default_terms_in_sync():
    assert addon.DEFAULT_TRANSLATION_TERMS == llm_mod.DEFAULT_TRANSLATION_TERMS
    assert "null pointer exception" in llm_mod.DEFAULT_TRANSLATION_TERMS


def test_path_constant_matches_core(monkeypatch):
    monkeypatch.undo()                                  # 比對真正的常數,不是 conftest 導向 tmp 後的值
    assert addon.TRANSLATION_TERMS_PATH == llm_mod.TERMS_PATH
    assert llm_mod.TERMS_PATH.endswith("translation_terms.json")


# ── 檔案層：兩份實作（core／addon）行為必須一致 ─────────────────────────────
IMPLS = [
    pytest.param((llm_mod, "TERMS_PATH"), id="core"),
    pytest.param((addon, "TRANSLATION_TERMS_PATH"), id="addon"),
]


@pytest.fixture(params=IMPLS)
def impl(request):
    mod, path_attr = request.param
    return mod, getattr(mod, path_attr)


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        f.write(obj if isinstance(obj, str) else json.dumps(obj))


class TestLoad:
    def test_missing_file_gives_defaults(self, impl):
        mod, path = impl
        d = mod.load_translation_terms(path)
        assert d == {"terms": list(mod.DEFAULT_TRANSLATION_TERMS), "pending": []}

    def test_default_is_a_copy(self, impl):
        mod, path = impl
        mod.load_translation_terms(path)["terms"].append("zzz")
        assert "zzz" not in mod.DEFAULT_TRANSLATION_TERMS

    @pytest.mark.parametrize("bad", ["{not json", "[]", '{"terms": "x"}',
                                     '{"terms": ["a"], "pending": "x"}', '{"terms": [1]}'])
    def test_bad_file_gives_defaults(self, impl, bad):
        mod, path = impl
        _write(path, bad)
        assert mod.load_translation_terms(path) == {
            "terms": list(mod.DEFAULT_TRANSLATION_TERMS), "pending": []}

    def test_good_file_roundtrip(self, impl):
        mod, path = impl
        pend = [{"term": "a b", "word": "w", "translation": "中文 a b"}]
        _write(path, {"terms": ["foo bar"], "pending": pend})
        assert mod.load_translation_terms(path) == {"terms": ["foo bar"], "pending": pend}

    def test_default_path_arg(self, impl):
        mod, path = impl
        _write(path, {"terms": ["foo bar"], "pending": []})
        assert mod.load_translation_terms()["terms"] == ["foo bar"]


class TestSave:
    def test_dedupe_lower_strip(self, impl):
        mod, path = impl
        ok = mod.save_translation_terms(
            {"terms": [" Foo Bar ", "foo bar", "", "  ", "Baz Qux"],
             "pending": [{"term": "x y", "word": "a", "translation": "1"},
                         {"term": "x y", "word": "b", "translation": "2"}]}, path)
        assert ok is True
        d = json.load(open(path, encoding="utf-8"))
        assert d["terms"] == ["foo bar", "baz qux"]
        assert [p["term"] for p in d["pending"]] == ["x y"]

    def test_failure_returns_false(self, impl, tmp_path):
        mod, _ = impl
        bad = str(tmp_path / "nope" / "t.json")
        assert mod.save_translation_terms({"terms": [], "pending": []}, bad) is False


class TestRecord:
    def test_records_phrase(self, impl):
        mod, path = impl
        _write(path, {"terms": [], "pending": []})
        got = mod.record_rejected_translation("dealing with", "我現在正處理 null pointer exception。", path)
        assert got == ["null pointer exception"]
        d = json.load(open(path, encoding="utf-8"))
        assert d["pending"] == [{"term": "null pointer exception", "word": "dealing with",
                                 "translation": "我現在正處理 null pointer exception。"}]

    def test_no_duplicate_pending(self, impl):
        mod, path = impl
        _write(path, {"terms": [], "pending": []})
        mod.record_rejected_translation("w", "我 null pointer exception。", path)
        assert mod.record_rejected_translation("w", "他 Null Pointer Exception。", path) == []
        assert len(json.load(open(path, encoding="utf-8"))["pending"]) == 1

    def test_known_term_not_recorded(self, impl):
        mod, path = impl
        _write(path, {"terms": ["null pointer exception"], "pending": []})
        assert mod.record_rejected_translation("w", "我 null pointer exception。", path) == []

    def test_preamble_becomes_pending(self, impl):
        mod, path = impl
        _write(path, {"terms": [], "pending": []})
        got = mod.record_rejected_translation("w", "Here is the translation: 我現在正處理空指標例外。", path)
        assert got == ["here is the translation"]

    def test_single_word_writes_nothing(self, impl):
        mod, path = impl
        import os
        assert mod.record_rejected_translation("w", "系統能處理 concurrency。", path) == []
        assert not os.path.exists(path)

    def test_first_write_seeds_defaults(self, impl):
        mod, path = impl
        mod.record_rejected_translation("w", "我 foo bar baz。", path)
        d = json.load(open(path, encoding="utf-8"))
        assert d["terms"] == list(mod.DEFAULT_TRANSLATION_TERMS)

    def test_strips_edge_punctuation(self, impl):
        mod, path = impl
        _write(path, {"terms": [], "pending": []})
        got = mod.record_rejected_translation("w", "他說 (Spring Boot), 好。", path)
        assert got == ["spring boot"]


class TestValidatorUsesTerms:
    def test_explicit_terms(self, impl):
        mod, _ = impl
        text = "我正在處理 foo bar baz 問題。"
        assert not mod._looks_like_chinese_translation(text, terms=[])
        assert mod._looks_like_chinese_translation(text, terms=["foo bar baz"])

    def test_empty_terms_runs(self, impl):
        mod, _ = impl
        assert mod._looks_like_chinese_translation("這是中文。", terms=[])

    def test_reads_user_file(self, impl):
        mod, path = impl
        text = "我正在處理 foo bar baz 問題。"
        _write(path, {"terms": [], "pending": []})
        assert not mod._looks_like_chinese_translation(text)
        _write(path, {"terms": ["foo bar baz"], "pending": []})        # 不快取：改檔立即生效
        assert mod._looks_like_chinese_translation(text)


class TestLlmTranslateRecords:
    def test_rejected_reply_is_recorded(self, monkeypatch, _terms_path):
        monkeypatch.setattr(llm_mod, "llm", lambda prompt: "Here is the translation: 我很忙。")
        assert llm_mod.llm_translate_sentence("I am busy.", word="busy") == ""
        d = json.load(open(_terms_path, encoding="utf-8"))
        assert [(p["term"], p["word"]) for p in d["pending"]] == [("here is the translation", "busy")]

    def test_accepted_reply_writes_nothing(self, monkeypatch, _terms_path):
        import os
        monkeypatch.setattr(llm_mod, "llm", lambda prompt: "我很忙。")
        assert llm_mod.llm_translate_sentence("I am busy.", word="busy") == "我很忙。"
        assert not os.path.exists(_terms_path)

    def test_addon_worker_collects_new_terms(self, monkeypatch, _terms_path):
        monkeypatch.setattr(addon, "_groq_chat", lambda *a, **k: "Here is the translation: 我很忙。")
        w = addon.Worker.__new__(addon.Worker)
        w.new_pending_terms = []
        assert w._groq_translate_sentence("I am busy.", word="busy") == ""
        assert w.new_pending_terms == ["here is the translation"]


@pytest.fixture
def _terms_path():
    return llm_mod.TERMS_PATH                          # conftest 已導到 tmp


class TestHardening:
    def test_concurrent_records_keep_approved_term(self, impl):
        import threading
        mod, path = impl
        _write(path, {"terms": ["my approved term"], "pending": []})

        def work(t):
            for i in range(50):
                tag = "".join(chr(97 + int(c)) for c in f"{t}{i:02d}")      # 只有字母才會被當成片語
                mod.record_rejected_translation("w", f"我 foo{tag} bar{tag} 。".replace("foo" + tag, "foo " + tag), path)

        ths = [threading.Thread(target=work, args=(t,)) for t in range(3)]
        [t.start() for t in ths]; [t.join() for t in ths]
        d = json.load(open(path, encoding="utf-8"))
        assert "my approved term" in d["terms"]
        assert len(d["pending"]) == 150

    def test_no_chinese_not_recorded(self, impl):
        mod, path = impl
        assert mod.load_translation_terms(path)["pending"] == []
        import os
        if mod is llm_mod:
            llm_mod_llm = lambda prompt: "I'm dealing with a null pointer exception now."
            import pytest as _p
            mp = _p.MonkeyPatch(); mp.setattr(llm_mod, "llm", llm_mod_llm)
            try:
                assert llm_mod.llm_translate_sentence("x y.", word="w") == ""
            finally:
                mp.undo()
        else:
            mp = pytest.MonkeyPatch()
            mp.setattr(addon, "_groq_chat", lambda *a, **k: "I'm dealing with a null pointer exception now.")
            try:
                w = addon.Worker.__new__(addon.Worker); w.new_pending_terms = []
                assert w._groq_translate_sentence("x y.", word="w") == ""
                assert w.new_pending_terms == []
            finally:
                mp.undo()
        assert not os.path.exists(path)

    def test_bad_file_backed_up(self, impl):
        import os
        mod, path = impl
        _write(path, "{oops")
        mod.load_translation_terms(path)
        mod.save_translation_terms({"terms": ["a b"], "pending": []}, path)
        assert open(path + ".bak", encoding="utf-8").read() == "{oops"
        assert json.load(open(path, encoding="utf-8"))["terms"] == ["a b"]

    def test_longest_term_wins(self, impl):
        mod, _ = impl
        text = "請看 unit test coverage report summary 報告。"
        assert mod._looks_like_chinese_translation(text, terms=["unit test", "unit test coverage"])
        assert not mod._looks_like_chinese_translation(text, terms=["unit test"])
