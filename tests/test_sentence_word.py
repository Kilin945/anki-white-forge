"""例句要含單字（卡片背面才高亮得到）：偵測規則跟 templates/back.html 一致——
單字是句子的子字串、不分大小寫（模板是 `new RegExp(word + "[a-z]*", "gi")`）。
偵測到沒有**不退句子**（sweep→swept 這種合法句子不能誤殺），而是帶著「必須包含這個字」
重問一次；重問仍沒有就照收、只記 log。core 與 addon 各一份，KEEP-IN-SYNC。
"""
from unittest.mock import patch

import addon
import core.llm as llm_mod
import core.text as text_mod


class TestHasWord:
    def test_exact_and_inflected_prefix(self):
        assert text_mod.sentence_has_word("penguin", "The penguins slid on the ice.")
        assert addon._sentence_has_word("penguin", "The penguins slid on the ice.")

    def test_case_insensitive(self):
        assert text_mod.sentence_has_word("penguin", "Penguin colonies are loud.")

    def test_irregular_form_is_not_matched_like_the_template(self):
        assert not text_mod.sentence_has_word("sweep", "She swept the crumbs off the table.")
        assert not addon._sentence_has_word("sweep", "She swept the crumbs off the table.")

    def test_phrase_must_appear_whole(self):
        assert text_mod.sentence_has_word("figure out", "I will figure out the bug.")
        assert not text_mod.sentence_has_word("figure out", "I figured it out.")

    def test_html_and_nbsp_in_word_are_ignored(self):
        assert text_mod.sentence_has_word("<b>penguin</b>&nbsp;", "A penguin waddled by.")
        assert addon._sentence_has_word("<b>penguin</b>&nbsp;", "A penguin waddled by.")

    def test_empty(self):
        assert not text_mod.sentence_has_word("", "A penguin.")
        assert not text_mod.sentence_has_word("penguin", "")


class TestPromptAddendum:
    def test_core_and_addon_prompts_match_with_must_contain(self):
        assert addon._sentence_prompt("penguin", "", "", must_contain=True) == (
            llm_mod._sentence_instructions("penguin", "", "", must_contain=True)
            + "\n\nOutput only the sentence. No explanation, no quotes.")

    def test_addendum_names_the_exact_word(self):
        p = llm_mod._sentence_instructions("penguin", must_contain=True)
        assert 'must contain the exact word "penguin"' in p
        assert "must contain the exact word" not in llm_mod._sentence_instructions("penguin")


class TestRetryCore:
    def test_retries_once_when_word_missing_and_uses_retry(self):
        replies = iter(["The tuxedoed bird waddled across the ice.", "The penguin waddled across the ice."])
        seen = []
        def fake_llm(prompt, **kw):
            seen.append(prompt); return next(replies)
        with patch.object(llm_mod, "llm", fake_llm):
            assert llm_mod.llm_sentence("penguin") == "The penguin waddled across the ice."
        assert len(seen) == 2 and "must contain the exact word" in seen[1]

    def test_no_retry_when_word_present(self):
        seen = []
        def fake_llm(prompt, **kw):
            seen.append(prompt); return "The penguin waddled across the ice."
        with patch.object(llm_mod, "llm", fake_llm):
            llm_mod.llm_sentence("penguin")
        assert len(seen) == 1

    def test_keeps_retry_even_if_word_still_missing(self):
        replies = iter(["She swept the floor today.", "She swept the floor again."])
        with patch.object(llm_mod, "llm", lambda p, **kw: next(replies)):
            assert llm_mod.llm_sentence("sweep") == "She swept the floor again."

    def test_falls_back_to_first_when_retry_is_junk(self):
        replies = iter(["She swept the floor today.", "sure, here you go:\nno"])
        with patch.object(llm_mod, "llm", lambda p, **kw: next(replies)):
            assert llm_mod.llm_sentence("sweep") == "She swept the floor today."


class TestRetryAddon:
    def _worker(self):
        w = addon.Worker.__new__(addon.Worker)
        w.word, w.association, w.media_dir = "penguin", "", "/tmp"
        return w

    def test_retries_once_when_word_missing(self):
        replies = iter(["The tuxedoed bird waddled across the ice.", "The penguin waddled across the ice."])
        seen = []
        def fake_chat(prompt, **kw):
            seen.append(prompt); return next(replies)
        with patch.object(addon, "_groq_chat", fake_chat):
            assert self._worker()._llm_sentence("penguin") == ("The penguin waddled across the ice.", "Groq")
        assert len(seen) == 2 and "must contain the exact word" in seen[1]

    def test_no_retry_when_word_present(self):
        seen = []
        with patch.object(addon, "_groq_chat", lambda p, **kw: seen.append(p) or "The penguin waddled by."):
            assert self._worker()._llm_sentence("penguin")[0] == "The penguin waddled by."
        assert len(seen) == 1
