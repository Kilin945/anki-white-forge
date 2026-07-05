"""頂層腳本 import 冒煙測試 — 釘住 core.llm 公開符號的完整消費者清單。

背景:core/llm.py 移除 _groq_client 時,漏盤了 add_word.py 與 _image_helper.py
兩個消費者(146 個測試都沒接到),merge 前的最終審查才抓到 → 用這支測試釘死。
"""
import importlib

import pytest


@pytest.mark.parametrize("mod", [
    "add_word",
    "_image_helper",
    "backfill_words",
    "backfill_sentence_cn",
])
def test_script_imports(mod):
    importlib.import_module(mod)
