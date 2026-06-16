"""core.llm prompt-construction tests — verify meaning-selection + quality rules are
present in the prompts, WITHOUT calling the real LLM (patch the llm() dispatcher)."""
from unittest.mock import patch
import core.llm as llm_mod


def _capture(fn, *args):
    """Call fn with llm() patched to record the prompt and return a valid 2-line reply."""
    seen = {}
    def fake_llm(prompt):
        seen["prompt"] = prompt
        return "A developer follows the team's naming convention here.\nnaming convention code screen"
    with patch.object(llm_mod, "llm", fake_llm):
        fn(*args)
    return seen["prompt"]


class TestSentencePrompt:
    def test_includes_swe_then_everyday_priority(self):
        p = _capture(llm_mod.llm_sentence, "convention")
        assert "software engineering" in p
        assert "everyday meaning" in p

    def test_bans_definition_or_circular_sentence(self):
        p = _capture(llm_mod.llm_sentence, "convention")
        assert "Do NOT write a definition" in p
        assert "X means" in p

    def test_uses_association_hint_when_given(self):
        p = _capture(llm_mod.llm_sentence, "convention", "coding standard")
        assert "coding standard" in p
        assert "Hint:" in p

    def test_no_hint_line_without_association(self):
        p = _capture(llm_mod.llm_sentence, "convention")
        assert "Hint:" not in p

    def test_brevity_target_and_no_forced_software(self):
        p = _capture(llm_mod.llm_sentence, "garden")
        assert "6-12 words" in p
        assert "do NOT force in software" in p


class TestSentenceAndQueryPrompt:
    def test_uses_shared_instructions_and_two_lines(self):
        p = _capture(llm_mod.llm_sentence_and_query, "thread", "execution unit")
        assert "software engineering" in p
        assert "execution unit" in p
        assert "Line 1" in p and "Line 2" in p

    def test_existing_sentence_keeps_same_meaning(self):
        p = _capture(llm_mod.llm_sentence_and_query, "thread", "", "A thread can run concurrently.")
        assert "SAME meaning" in p
        assert "A thread can run concurrently." in p


class TestTranslatePrompt:
    def test_follows_sentence_sense(self):
        p = _capture(llm_mod.llm_translate, "convention", "We follow a naming convention.")
        assert "as it is used in this sentence" in p
        assert "naming convention" in p

    def test_bans_synonym_lists(self):
        p = _capture(llm_mod.llm_translate, "cup", "She filled the cup with tea.")
        assert "near-duplicate" in p

    def test_keeps_proper_noun_in_english(self):
        p = _capture(llm_mod.llm_translate, "Spring", "We use the Spring framework.")
        assert "proper noun" in p
        assert "output the English name" in p


class TestAcceptWordTranslation:
    def test_accepts_chinese_gloss(self):
        assert llm_mod._accept_word_translation("convention", "慣例") == "慣例"

    def test_accepts_multiword_proper_noun_that_echoes_word(self):
        assert llm_mod._accept_word_translation("spring", "Spring Boot") == "Spring Boot"
        assert llm_mod._accept_word_translation("kafka", "Apache Kafka") == "Apache Kafka"

    def test_rejects_english_junk_not_echoing_word(self):
        assert llm_mod._accept_word_translation("reliable", "None") == ""
        assert llm_mod._accept_word_translation("convention", "I cannot translate this") == ""

    def test_rejects_chinese_sentence_too_long(self):
        assert llm_mod._accept_word_translation("x", "這是一個非常長的句子翻譯超過了八個字元") == ""

    def test_rejects_chinese_with_english_preamble(self):
        assert llm_mod._accept_word_translation("x", "翻譯是 the convention here") == ""
