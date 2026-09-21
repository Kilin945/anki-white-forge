"""LLM 洩漏防線：Gemini thought part 過濾 + 造句結構守門的純邏輯測試。

事故（2026-09-22）：兩張真卡的 Sentence 欄位存進的不是例句，而是模型的思考中段
與被回吐的 prompt。它們非空、非佔位符、長度 > 10 → 舊守門放行 → 寫進卡片假裝
成合法句子。之後 Sentence_CN 拿它去翻，翻出來必含 3+ 英文詞被驗證擋掉，而句子
「看起來合法」所以 ⌘S 不會重生 → 使用者重跑幾次都修不好。

兩道防線：
  1. `_extract_gemini_text` 只收非 thought 的 part（堵源頭）
  2. `sentence_acceptable` 結構性守門（即使源頭再漏，髒句子也進不了卡片）

addon 會 import Anki 的 aqt（測試環境沒有）→ 假 aqt 由 conftest.py 統一安裝。
"""
import addon  # 假 aqt 已由 conftest.py 安裝
import addon._llm_dispatch as addon_dispatch
from core import providers as core_providers
from core.text import MAX_SENTENCE_WORDS, sentence_acceptable

# 兩個真實事故樣本（逐字取自壞掉的卡片）
LEAK_THOUGHT = ('cause to collapse/stop functioning) might not be ideal unless requested. '
                'But wait, "brought down" is very common in tech. However, "bring" itself '
                'is not a tech term. "Otherwise use its most common everyday meaning."')
LEAK_PROMPT = ("priority: 1. If 'precise' has a common usage in software engineering / "
               'programming / tech, use that sense. 2. Otherwise use its most common '
               'everyday meaning."\n                *   In tech, "precise" usually means exact')

# 牌組裡真實存在的正常句子（含 "We need to ..." 這種曾被誤判為思考語氣的開頭）
REAL_SENTENCES = [
    "She brought the coffee to the meeting.",
    "The patch brought a crash on startup.",
    "We need to prepare for every possible scenario.",
    "We need to nail down the API specification before release.",
    "We need to throttle incoming API requests.",
    "The latest commit brought the server online.",
]


def _both_gates():
    """core 與 addon 兩份實作（KEEP-IN-SYNC）都要套同一組案例。"""
    return [("core", sentence_acceptable), ("addon", addon._sentence_acceptable)]


class TestSentenceAcceptable:
    def test_real_sentences_pass(self):
        for name, gate in _both_gates():
            for s in REAL_SENTENCES:
                assert gate(s) is True, f"{name} rejected a real sentence: {s!r}"

    def test_thought_leak_rejected(self):
        for name, gate in _both_gates():
            assert gate(LEAK_THOUGHT) is False, f"{name} accepted the thought leak"

    def test_prompt_leak_rejected(self):
        for name, gate in _both_gates():
            assert gate(LEAK_PROMPT) is False, f"{name} accepted the prompt leak"

    def test_lowercase_start_rejected(self):
        # 洩漏多半是從長文中段截斷 → 開頭是半句小寫
        for name, gate in _both_gates():
            assert gate("cause to collapse and stop working here.") is False, name

    def test_multiline_rejected(self):
        for name, gate in _both_gates():
            assert gate("She brought coffee.\nIt was hot.") is False, name

    def test_word_count_boundary(self):
        at_limit = "She " + "really " * (MAX_SENTENCE_WORDS - 2) + "went."
        over_limit = "She " + "really " * (MAX_SENTENCE_WORDS - 1) + "went."
        for name, gate in _both_gates():
            assert len(at_limit.split()) == MAX_SENTENCE_WORDS
            assert gate(at_limit) is True, f"{name} rejected a sentence at the limit"
            assert gate(over_limit) is False, f"{name} accepted an over-limit sentence"

    def test_empty_and_none_rejected(self):
        for name, gate in _both_gates():
            assert gate("") is False, name
            assert gate(None) is False, name
            assert gate("   ") is False, name

    def test_too_short_rejected(self):
        # 保留舊 len>10 的下限
        for name, gate in _both_gates():
            assert gate("She went.") is False, name


def _gemini_payload(parts):
    return {"candidates": [{"content": {"parts": parts}}]}


class TestGeminiThoughtFiltering:
    """thinkingConfig 開著時 parts 會夾帶 thought 段落；盲取 parts[0] 就是事故源頭。"""

    def _both_extractors(self):
        return [("core", core_providers._extract_gemini_text),
                ("addon", addon_dispatch._extract_gemini_text)]

    def test_thought_part_is_skipped(self):
        data = _gemini_payload([
            {"text": "But wait, brought down is common in tech.", "thought": True},
            {"text": "She brought the coffee to the meeting."},
        ])
        for name, extract in self._both_extractors():
            assert extract(data) == "She brought the coffee to the meeting.", name

    def test_thought_only_response_yields_empty(self):
        # 思考吃光預算、沒有真正答案 → 回 ''，呼叫端視為失敗（而不是把思考當答案）
        data = _gemini_payload([{"text": LEAK_THOUGHT, "thought": True}])
        for name, extract in self._both_extractors():
            assert extract(data) == "", name

    def test_plain_response_still_works(self):
        data = _gemini_payload([{"text": "She brought the coffee."}])
        for name, extract in self._both_extractors():
            assert extract(data) == "She brought the coffee.", name

    def test_multiple_answer_parts_are_joined(self):
        data = _gemini_payload([
            {"text": "thinking...", "thought": True},
            {"text": "line one"},
            {"text": "line two"},
        ])
        for name, extract in self._both_extractors():
            assert extract(data) == "line one\nline two", name

    def test_bad_shapes_yield_empty(self):
        for name, extract in self._both_extractors():
            assert extract({}) == "", name
            assert extract({"candidates": []}) == "", name
            assert extract(_gemini_payload(None)) == "", name
            assert extract({"candidates": [{"content": {}}]}) == "", name

    def test_non_string_text_ignored(self):
        data = _gemini_payload([{"text": None}, {"text": "She brought coffee here."}])
        for name, extract in self._both_extractors():
            assert extract(data) == "She brought coffee here.", name
