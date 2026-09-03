"""句子失敗擋下游（gate）+ 造句思考等級的 addon 端純邏輯測試。

規則：句子不可用（空 / 佔位符）時，依賴句意的下游（Image / Translation /
Sentence_CN / 句音）全部跳過，只做與句子無關的 Front_Audio；下次 ⌘S 一起重來。
判斷抽成純函式 `_sentence_usable`。造句呼叫走 effort="medium"（其餘預設 low）。

addon 會 import Anki 的 aqt（測試環境沒有）→ 假 aqt 由 conftest.py 統一安裝。
"""
from unittest.mock import patch

import addon  # 假 aqt 已由 conftest.py 安裝


class TestSentenceUsable:
    def test_real_sentence_is_usable(self):
        assert addon._sentence_usable("The cat sat on the mat.") is True

    def test_empty_is_not_usable(self):
        assert addon._sentence_usable("") is False

    def test_placeholder_is_not_usable(self):
        for p in addon.PLACEHOLDERS:
            assert addon._sentence_usable(f"xx {p} yy") is False

    def test_addon_failure_placeholder_is_not_usable(self):
        # ⌘A / ⌘S 生成失敗時寫入的實際佔位符句
        assert addon._sentence_usable(
            "Please add an example sentence for 'cat'.") is False


class _RecorderDispatcher:
    def __init__(self, reply="ok"):
        self.providers = ["x"]
        self.kwargs = None
        self._reply = reply

    def generate(self, prompt, **kw):
        self.kwargs = kw
        return self._reply

    def wall_secs(self):
        return 0.0


class TestEffortCallSites:
    def test_groq_chat_defaults_to_low(self):
        rec = _RecorderDispatcher()
        with patch.object(addon, "_dispatcher", rec):
            addon._groq_chat("p", temperature=0, max_tokens=8, timeout=5)
        assert rec.kwargs["effort"] == "low"

    def test_groq_chat_forwards_effort(self):
        rec = _RecorderDispatcher()
        with patch.object(addon, "_dispatcher", rec):
            addon._groq_chat("p", temperature=0, max_tokens=8, timeout=5,
                             effort="medium")
        assert rec.kwargs["effort"] == "medium"

    def test_sentence_generation_uses_medium_effort(self):
        rec = _RecorderDispatcher(reply="A cat sat on the warm mat.")
        with patch.object(addon, "_dispatcher", rec):
            addon.Worker._groq_sentence(None, "cat")
        assert rec.kwargs["effort"] == "medium"

    def test_word_translation_stays_low_effort(self):
        rec = _RecorderDispatcher(reply="貓")
        with patch.object(addon, "_dispatcher", rec):
            addon.Worker._groq_translate(None, "cat", "A cat sat.")
        assert rec.kwargs["effort"] == "low"
