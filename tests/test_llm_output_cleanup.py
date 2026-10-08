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
