"""LLM 回覆裡的控制字元（實測 Groq 回過 'penguin on ice\\x00'）在 dispatcher 統一清掉，
core 與 addon 兩份 KEEP-IN-SYNC；換行與 tab 保留（句子守門靠換行擋洩漏）。"""
import importlib.util
import pathlib

import core.dispatcher as disp

_SPEC = importlib.util.spec_from_file_location(
    "lld", pathlib.Path(__file__).parent.parent / "addon" / "_llm_dispatch.py")
lld = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lld)


class _P:
    name = "fake"
    def __init__(self, reply): self._reply = reply
    def headroom(self): return 1.0
    def reset_secs(self): return 0.0
    def generate(self, prompt, **kw): return self._reply


def test_core_strips_nul_and_control_chars_but_keeps_newline():
    d = disp.Dispatcher([_P("penguin on ice\x00\x07\nnext\tline")])
    assert d.generate("x") == "penguin on ice\nnext\tline"


def test_addon_strips_nul_and_control_chars_but_keeps_newline():
    d = lld.Dispatcher([_P("penguin on ice\x00\x07\nnext\tline")])
    assert d.generate("x", temperature=0.3, max_tokens=32, timeout=10) == "penguin on ice\nnext\tline"


def test_clean_functions_match():
    for s in ["a\x00b", "a\x1fb\x7fc", "ok\n", "", None]:
        assert disp.clean_llm_text(s) == lld.clean_llm_text(s)


# 特殊空白（2026-10-09 實例：例句與整句翻譯出現 'Van der Pol'）：U+202F 不算空格，
# 抓術語的 regex 把它切成 'van der' + 'Pol'，白名單手動加 'van der pol' 也對不上 → 卡永遠補不出來。
def test_core_turns_special_spaces_into_plain_spaces():
    d = disp.Dispatcher([_P("我們模擬 Van der Pol 振盪器，10 km　以內。")])
    assert d.generate("x") == "我們模擬 Van der Pol 振盪器，10 km 以內。"


def test_addon_turns_special_spaces_into_plain_spaces():
    d = lld.Dispatcher([_P("We simulate the Van der Pol oscillator.")])
    assert d.generate("x", temperature=0.3, max_tokens=32, timeout=10) == \
        "We simulate the Van der Pol oscillator."


def test_zero_width_chars_are_removed_not_spaced():
    assert lld.clean_llm_text("simu​late﻿") == "simulate"


def test_cleaned_translation_matches_whitelisted_phrase():
    import core.llm
    raw = "我們為分析而模擬 Van der Pol 振盪器。"
    assert not core.llm._looks_like_chinese_translation(raw, ["van der pol"])
    assert core.llm._looks_like_chinese_translation(disp.clean_llm_text(raw), ["van der pol"])


def test_special_space_cleanup_matches_between_core_and_addon():
    for s in ["a b", "a b c", "x​y﻿", "全形　空白"]:
        assert disp.clean_llm_text(s) == lld.clean_llm_text(s)
