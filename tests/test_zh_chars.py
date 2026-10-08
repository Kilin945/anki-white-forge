"""擋簡體：簡體獨有字表（core／addon 兩份 KEEP-IN-SYNC）與四個翻譯函式的轉換行為。"""
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

import addon
import addon._zh_chars as a_zh
import core.llm as llm_mod
import core.zh_chars as c_zh

MODS = [pytest.param(c_zh, id="core"), pytest.param(a_zh, id="addon")]


def test_tables_equal_and_count():
    assert a_zh.SIMPLIFIED_TO_TRADITIONAL == c_zh.SIMPLIFIED_TO_TRADITIONAL
    assert len(c_zh.SIMPLIFIED_TO_TRADITIONAL) == 3796


def test_files_identical_text():
    with open(c_zh.__file__, encoding="utf-8") as a, open(a_zh.__file__, encoding="utf-8") as b:
        assert a.read() == b.read()


@pytest.mark.parametrize("m", MODS)
class TestTable:
    def test_taiwan_standard_chars_not_in_table(self, m):
        for ch in "台吃了才布秘回峰周群郁弦痴家具后干":
            assert ch not in m.SIMPLIFIED_TO_TRADITIONAL, ch

    def test_simplified_only_chars_map_correctly(self, m):
        for s, t in "犷獷 旧舊 响響 个個 为為 发發 针針 异異 处處 这這 们們 给給 对對".split():
            assert m.SIMPLIFIED_TO_TRADITIONAL[s] == t

    def test_has_simplified(self, m):
        assert m.has_simplified("空指针异常")
        assert not m.has_simplified("空指針異常")
        assert not m.has_simplified("")
        assert not m.has_simplified(None)

    def test_to_traditional(self, m):
        assert m.to_traditional("她揮動手做出了一个手勢。") == "她揮動手做出了一個手勢。"
        assert m.to_traditional("全繁體不變，陽台、吃、了解") == "全繁體不變，陽台、吃、了解"

    def test_simplified_chars_dedup_ordered(self, m):
        assert m.simplified_chars("粗犷破旧犷") == ["犷", "旧"]
        assert m.simplified_chars("") == []


def test_generator_check_passes():
    cmd = ["uv", "run", "--with", "opencc-python-reimplemented", "python",
           "tools/gen_zh_chars.py", "--check"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                           cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # tests/ → repo 根目錄
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("cannot run uv")
    if r.returncode != 0 and "DIFFERENT" not in r.stdout:
        pytest.skip("opencc unavailable (offline?)")
    assert r.returncode == 0, r.stdout + r.stderr


# ── 翻譯函式：簡體回覆先轉繁再驗證 ────────────────────────────────────────────

class TestCoreWiring:
    def test_word_translation_converted(self):
        with patch.object(llm_mod, "llm", lambda *a, **k: "空指针异常"):
            assert llm_mod.llm_translate("npe", "x") == "空指針異常"

    def test_sentence_whitelist_still_passes(self):
        with patch.object(llm_mod, "llm", lambda *a, **k: "我現在正處理 null pointer exception。"):
            assert llm_mod.llm_translate_sentence("I am dealing with a null pointer exception.") \
                == "我現在正處理 null pointer exception。"

    def test_sentence_converted(self):
        with patch.object(llm_mod, "llm", lambda *a, **k: "她挥动手做出了一个手势。"):
            assert llm_mod.llm_translate_sentence("She waved.") == "她揮動手做出了一個手勢。"

    def test_prompts_demand_taiwan_traditional(self):
        seen = []
        with patch.object(llm_mod, "llm", lambda p, **k: seen.append(p) or "測試"):
            llm_mod.llm_translate("test", "a test")
            llm_mod.llm_translate_sentence("A test.")
        for p in seen:
            assert "Write Traditional Chinese as used in Taiwan; never use Simplified Chinese characters." in p
            assert p.index("never use Simplified") < p.index("Output only")


class TestAddonWiring:
    def _worker(self):
        return addon._workers.Worker.__new__(addon._workers.Worker)

    def test_word_translation_converted(self):
        with patch.object(addon._llm, "_groq_chat", lambda *a, **k: "空指针异常"):
            assert self._worker()._groq_translate("npe", "x") == "空指針異常"

    def test_sentence_whitelist_still_passes(self):
        with patch.object(addon._llm, "_groq_chat", lambda *a, **k: "我現在正處理 null pointer exception。"):
            assert self._worker()._groq_translate_sentence("I am dealing with it.") \
                == "我現在正處理 null pointer exception。"

    def test_sentence_converted(self):
        with patch.object(addon._llm, "_groq_chat", lambda *a, **k: "她挥动手做出了一个手势。"):
            assert self._worker()._groq_translate_sentence("She waved.") == "她揮動手做出了一個手勢。"

    def test_prompts_demand_taiwan_traditional(self):
        seen = []
        with patch.object(addon._llm, "_groq_chat", lambda p, **k: seen.append(p) or "測試"):
            w = self._worker()
            w._groq_translate("test", "a test")
            w._groq_translate_sentence("A test.")
        for p in seen:
            assert "Write Traditional Chinese as used in Taiwan; never use Simplified Chinese characters." in p
            assert p.index("never use Simplified") < p.index("Output only")


# ── check_simplified.py 的純函式 ──────────────────────────────────────────────

def test_check_simplified_find_and_fix_keeps_html():
    import check_simplified as cs
    notes = [
        {"noteId": 1, "fields": {"Front": {"value": "rugged"}, "Translation": {"value": "<b>粗犷</b>"},
                                 "Sentence_CN": {"value": "這是陽台。"}}},
        {"noteId": 2, "fields": {"Front": {"value": "ok"}, "Translation": {"value": "了解"},
                                 "Sentence_CN": {"value": ""}}},
    ]
    hits = cs.find_hits(notes)
    assert len(hits) == 1
    h = hits[0]
    assert (h["nid"], h["field"], h["chars"]) == (1, "Translation", "犷")
    assert h["new"] == "<b>粗獷</b>"
