"""Tests for core modules and scripts."""
import os
import json
import pytest
from unittest.mock import patch

from core.text import strip_html, normalize, is_placeholder, has_image
import core.llm as llm_mod
import core.image as img_mod
from core.tts import make_audio, VOICE_WORD
from core.rate_limiter import BatchLimiter, RateLimitReached, is_rate_limit_error


class TestStripHtml:
    def test_basic_tags(self):
        assert strip_html("<b>hello</b>") == "hello"
    def test_div_wrapper(self):
        assert strip_html("<div>audit</div>") == "audit"
    def test_html_entities(self):
        assert strip_html("don&apos;t") == "don't"
    def test_nbsp(self):
        assert strip_html("hello&nbsp;world") == "hello world"
    def test_non_breaking_space_char(self):
        assert strip_html("hello\xa0world") == "hello world"
    def test_nested_tags(self):
        assert strip_html('<div class="word"><b>test</b></div>') == "test"
    def test_empty_string(self):
        assert strip_html("") == ""
    def test_plain_text(self):
        assert strip_html("hello world") == "hello world"


class TestNormalize:
    def test_curly_single_quotes(self):
        assert normalize("‘hello’") == "'hello'"
    def test_curly_double_quotes(self):
        assert normalize("“hello”") == '"hello"'
    def test_em_dash(self):
        assert normalize("a—b") == "a-b"
    def test_en_dash(self):
        assert normalize("a–b") == "a-b"
    def test_nbsp_char(self):
        assert normalize("a\xa0b") == "a b"
    def test_normal_text_unchanged(self):
        assert normalize("hello world") == "hello world"


class TestIsPlaceholder:
    def test_placeholder_detected(self):
        assert is_placeholder("Please add an example sentence for 'word'.")
    def test_normal_sentence_not_placeholder(self):
        assert not is_placeholder("The cat sat on the mat.")
    def test_no_example_found(self):
        assert is_placeholder("No example found for this word.")
    def test_empty_not_placeholder(self):
        assert not is_placeholder("")


class TestHasImage:
    def test_img_tag(self):
        assert has_image('<img src="file.jpg">')
    def test_residual_html(self):
        assert not has_image('<div><br></div>')
    def test_empty(self):
        assert not has_image("")
    def test_with_attribution(self):
        assert has_image('<img src="f.jpg"><div>Photo by X</div>')


class TestLlmSentenceAndQuery:
    @patch.object(llm_mod, 'llm')
    def test_parses_two_lines(self, mock_llm):
        mock_llm.return_value = "The audit revealed discrepancies.\nfinancial audit review documents"
        sentence, query = llm_mod.llm_sentence_and_query("audit")
        assert len(sentence) > 10
        assert len(query) > 3

    @patch.object(llm_mod, 'llm')
    def test_single_line_fallback(self, mock_llm):
        mock_llm.return_value = "The audit revealed discrepancies."
        sentence, query = llm_mod.llm_sentence_and_query("audit")
        assert len(sentence) > 10
        assert "audit" in query

    @patch.object(llm_mod, 'llm')
    def test_empty_response_fallback(self, mock_llm):
        mock_llm.return_value = ""
        sentence, query = llm_mod.llm_sentence_and_query("audit")
        assert sentence == ""
        assert "audit" in query

    @patch.object(llm_mod, 'llm')
    def test_strips_quotes(self, mock_llm):
        mock_llm.return_value = '"The audit was thorough."\n"financial audit documents"'
        sentence, query = llm_mod.llm_sentence_and_query("audit")
        assert not sentence.startswith('"')
        assert not query.startswith('"')


class TestLlmFallback:
    @patch.object(llm_mod, 'ollama_generate')
    @patch.object(llm_mod, 'groq_generate')
    def test_groq_primary(self, mock_groq, mock_ollama):
        mock_groq.return_value = "groq result"
        assert llm_mod.llm("test") == "groq result"
        mock_ollama.assert_not_called()

    @patch.object(llm_mod, 'ollama_generate')
    @patch.object(llm_mod, 'groq_generate')
    def test_ollama_fallback(self, mock_groq, mock_ollama):
        mock_groq.return_value = ""
        mock_ollama.return_value = "ollama result"
        assert llm_mod.llm("test") == "ollama result"

    @patch.object(llm_mod, 'ollama_generate')
    @patch.object(llm_mod, 'groq_generate')
    def test_both_fail(self, mock_groq, mock_ollama):
        mock_groq.return_value = ""
        mock_ollama.return_value = ""
        assert llm_mod.llm("test") == ""


class TestGroqKeyLoading:
    def test_loads_from_file(self):
        client = llm_mod._load_groq_client()
        if os.path.exists(llm_mod.GROQ_KEY_PATH):
            assert client is not None


class TestPexelsKeyLoading:
    def test_loads_from_file(self):
        key = img_mod._load_pexels_key()
        if os.path.exists(img_mod.PEXELS_KEY_PATH):
            assert len(key) > 0


class TestMakeAudio:
    def test_generates_mp3(self, tmp_path):
        fp = str(tmp_path / "test.mp3")
        make_audio("hello world", fp)
        assert os.path.exists(fp) and os.path.getsize(fp) > 100

    def test_voice_parameter(self, tmp_path):
        fp = str(tmp_path / "test.mp3")
        make_audio("hello", fp, voice=VOICE_WORD)
        assert os.path.exists(fp)


class TestProcessNote:
    def _note(self, word, sentence="", image="", audio="", front_audio=""):
        return {"noteId": 12345, "fields": {
            "Front": {"value": word}, "Association": {"value": ""},
            "Sentence": {"value": sentence}, "Image_Prompt": {"value": image},
            "Audio": {"value": audio}, "Front_Audio": {"value": front_audio},
        }}

    def test_skips_complete(self):
        from backfill_words import process_note
        word, status = process_note(self._note("test", "A test.", "<img>", "[sound:x]", "[sound:y]"))
        assert status == "skipped"


class TestGttsHelper:
    def test_single_mode(self, tmp_path):
        import subprocess
        fp = str(tmp_path / "test.mp3")
        r = subprocess.run(["uv", "run", "python", "_gtts_helper.py", "hello", fp],
                           capture_output=True, text=True, timeout=20, cwd="/Users/yeqilin/Workspace/Anki")
        assert r.returncode == 0 and os.path.exists(fp)

    def test_batch_mode(self, tmp_path):
        import subprocess
        f1, f2 = str(tmp_path / "a.mp3"), str(tmp_path / "b.mp3")
        items = json.dumps([{"text": "hello", "filepath": f1}, {"text": "world", "filepath": f2}])
        r = subprocess.run(["uv", "run", "python", "_gtts_helper.py", "--batch", items],
                           capture_output=True, text=True, timeout=30, cwd="/Users/yeqilin/Workspace/Anki")
        assert r.returncode == 0 and os.path.exists(f1) and os.path.exists(f2)


class TestImageQuery:
    @patch.object(llm_mod, 'llm', return_value="test query photo")
    def test_with_definition(self, mock):
        result = llm_mod.llm_image_query("audit", definition="examine")
        assert len(result) > 3

    @patch.object(llm_mod, 'llm', return_value="")
    def test_fallback(self, mock):
        result = llm_mod.llm_image_query("audit")
        assert "audit" in result


class TestLlmTranslateSentence:
    @patch.object(llm_mod, 'llm')
    def test_returns_chinese(self, mock_llm):
        mock_llm.return_value = "這個系統能妥善處理併發。"
        assert llm_mod.llm_translate_sentence("The system handles concurrency well.") == "這個系統能妥善處理併發。"

    @patch.object(llm_mod, 'llm')
    def test_strips_wrapping_quotes(self, mock_llm):
        mock_llm.return_value = '"這是一隻貓。"'
        assert llm_mod.llm_translate_sentence("This is a cat.") == "這是一隻貓。"

    @patch.object(llm_mod, 'llm')
    def test_rejects_english_preamble(self, mock_llm):
        mock_llm.return_value = "Here is the translation: 這是一隻貓。"
        assert llm_mod.llm_translate_sentence("This is a cat.") == ""

    @patch.object(llm_mod, 'llm')
    def test_rejects_no_chinese(self, mock_llm):
        mock_llm.return_value = "I cannot translate this."
        assert llm_mod.llm_translate_sentence("foo") == ""

    def test_empty_sentence_returns_empty(self):
        assert llm_mod.llm_translate_sentence("") == ""


class TestBatchLimiter:
    def test_new_limiter_continues(self):
        lim = BatchLimiter(batch_limit=3)
        assert lim.should_continue() is True

    def test_stops_at_batch_limit(self):
        lim = BatchLimiter(batch_limit=2)
        lim.record_success(); lim.record_success()
        assert lim.should_continue() is False
        assert lim.stopped_reason == "batch_limit"

    def test_stops_on_rate_limited(self):
        lim = BatchLimiter(batch_limit=99)
        lim.record_rate_limited()
        assert lim.should_continue() is False
        assert lim.stopped_reason == "rate_limited"


class TestIsRateLimitError:
    def test_rate_limit_reached(self):
        assert is_rate_limit_error(RateLimitReached()) is True

    def test_code_429(self):
        e = Exception(); e.code = 429
        assert is_rate_limit_error(e) is True

    def test_status_code_429(self):
        e = Exception(); e.status_code = 429
        assert is_rate_limit_error(e) is True

    def test_message_contains_429(self):
        assert is_rate_limit_error(Exception("Error 429 rate limit")) is True

    def test_other_error_false(self):
        assert is_rate_limit_error(Exception("boom")) is False


import backfill_sentence_cn as bf_cn


def _cn_note(nid, sentence, cn=""):
    return {"noteId": nid, "fields": {
        "Sentence": {"value": sentence}, "Sentence_CN": {"value": cn}}}


class TestPendingNotes:
    def test_picks_missing_cn_with_sentence(self):
        notes = [_cn_note(1, "A cat.", ""), _cn_note(2, "A dog.", "一隻狗。")]
        assert [n["noteId"] for n in bf_cn.pending_notes(notes)] == [1]

    def test_skips_when_no_sentence(self):
        notes = [_cn_note(1, "", "")]
        assert bf_cn.pending_notes(notes) == []


class TestRunBatch:
    def test_stops_at_batch_limit(self):
        notes = [_cn_note(i, f"Sentence {i}.") for i in range(5)]
        updates = []
        lim = BatchLimiter(batch_limit=2)
        done, remaining = bf_cn.run_batch(
            notes, translate=lambda s: "譯文", update=lambda nid, cn: updates.append(nid), limiter=lim)
        assert done == 2
        assert remaining == 3
        assert lim.stopped_reason == "batch_limit"
        assert updates == [0, 1]

    def test_stops_on_rate_limit(self):
        notes = [_cn_note(i, f"Sentence {i}.") for i in range(5)]
        calls = {"n": 0}
        def translate(s):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RateLimitReached()
            return "譯文"
        lim = BatchLimiter(batch_limit=99)
        done, remaining = bf_cn.run_batch(
            notes, translate=translate, update=lambda nid, cn: None, limiter=lim)
        assert done == 1
        assert lim.stopped_reason == "rate_limited"

    def test_skips_empty_translation(self):
        notes = [_cn_note(1, "A cat."), _cn_note(2, "A dog.")]
        updates = []
        lim = BatchLimiter(batch_limit=99)
        done, remaining = bf_cn.run_batch(
            notes, translate=lambda s: "" if s == "A cat." else "一隻狗。",
            update=lambda nid, cn: updates.append(nid), limiter=lim)
        assert done == 1
        assert updates == [2]
