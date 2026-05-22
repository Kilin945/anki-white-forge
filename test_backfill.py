"""Tests for backfill_words.py and shared helpers."""
import os
import json
import pytest
from unittest.mock import patch, MagicMock

import backfill_words as bf


# ── Pure functions ──

class TestStripHtml:
    def test_basic_tags(self):
        assert bf.strip_html("<b>hello</b>") == "hello"

    def test_div_wrapper(self):
        assert bf.strip_html("<div>audit</div>") == "audit"

    def test_html_entities(self):
        assert bf.strip_html("don&apos;t") == "don't"

    def test_nbsp(self):
        assert bf.strip_html("hello&nbsp;world") == "hello world"

    def test_non_breaking_space_char(self):
        assert bf.strip_html("hello\xa0world") == "hello world"

    def test_nested_tags(self):
        assert bf.strip_html('<div class="word"><b>test</b></div>') == "test"

    def test_empty_string(self):
        assert bf.strip_html("") == ""

    def test_plain_text(self):
        assert bf.strip_html("hello world") == "hello world"


class TestNormalize:
    def test_curly_single_quotes(self):
        assert bf._normalize("‘hello’") == "'hello'"

    def test_curly_double_quotes(self):
        assert bf._normalize("“hello”") == '"hello"'

    def test_em_dash(self):
        assert bf._normalize("a—b") == "a-b"

    def test_en_dash(self):
        assert bf._normalize("a–b") == "a-b"

    def test_nbsp_char(self):
        assert bf._normalize("a\xa0b") == "a b"

    def test_normal_text_unchanged(self):
        assert bf._normalize("hello world") == "hello world"


class TestIsPlaceholder:
    def test_placeholder_detected(self):
        assert bf.is_placeholder("Please add an example sentence for 'word'.")

    def test_normal_sentence_not_placeholder(self):
        assert not bf.is_placeholder("The cat sat on the mat.")

    def test_no_example_found(self):
        assert bf.is_placeholder("No example found for this word.")

    def test_empty_not_placeholder(self):
        assert not bf.is_placeholder("")


# ── LLM response parsing ──

class TestLlmSentenceAndQuery:
    @patch.object(bf, 'llm')
    def test_parses_two_lines(self, mock_llm):
        mock_llm.return_value = "The audit revealed discrepancies.\nfinancial audit review documents"
        sentence, query = bf.llm_sentence_and_query("audit")
        assert "audit" in sentence.lower() or "discrepancies" in sentence.lower()
        assert len(query) > 3

    @patch.object(bf, 'llm')
    def test_single_line_fallback(self, mock_llm):
        mock_llm.return_value = "The audit revealed discrepancies."
        sentence, query = bf.llm_sentence_and_query("audit")
        assert "audit" in sentence.lower() or "discrepancies" in sentence.lower()
        assert "audit" in query

    @patch.object(bf, 'llm')
    def test_empty_response_fallback(self, mock_llm):
        mock_llm.return_value = ""
        sentence, query = bf.llm_sentence_and_query("audit")
        assert sentence == ""
        assert "audit" in query

    @patch.object(bf, 'llm')
    def test_with_definition(self, mock_llm):
        mock_llm.return_value = "They will audit the accounts.\naccounting financial review"
        sentence, query = bf.llm_sentence_and_query("audit", definition="examine carefully")
        assert sentence != ""
        assert query != ""

    @patch.object(bf, 'llm')
    def test_strips_quotes_from_output(self, mock_llm):
        mock_llm.return_value = '"The audit was thorough."\n"financial audit documents"'
        sentence, query = bf.llm_sentence_and_query("audit")
        assert not sentence.startswith('"')
        assert not query.startswith('"')


# ── LLM fallback chain ──

class TestLlmFallback:
    @patch.object(bf, 'ollama_generate')
    @patch.object(bf, 'groq_generate')
    def test_groq_primary(self, mock_groq, mock_ollama):
        mock_groq.return_value = "groq result"
        result = bf.llm("test prompt")
        assert result == "groq result"
        mock_ollama.assert_not_called()

    @patch.object(bf, 'ollama_generate')
    @patch.object(bf, 'groq_generate')
    def test_ollama_fallback(self, mock_groq, mock_ollama):
        mock_groq.return_value = ""
        mock_ollama.return_value = "ollama result"
        result = bf.llm("test prompt")
        assert result == "ollama result"

    @patch.object(bf, 'ollama_generate')
    @patch.object(bf, 'groq_generate')
    def test_both_fail(self, mock_groq, mock_ollama):
        mock_groq.return_value = ""
        mock_ollama.return_value = ""
        result = bf.llm("test prompt")
        assert result == ""


# ── Groq client loading ──

class TestGroqKeyLoading:
    def test_loads_from_file(self):
        client = bf._load_groq_client()
        if os.path.exists(bf.GROQ_KEY_PATH):
            assert client is not None
        # if no key file, client may be None — that's OK


# ── Pexels key loading ──

class TestPexelsKeyLoading:
    def test_loads_from_file(self):
        key = bf._load_pexels_key()
        if os.path.exists(bf.PEXELS_KEY_PATH):
            assert len(key) > 0


# ── Audio generation ──

class TestMakeAudio:
    def test_generates_mp3_file(self, tmp_path):
        filepath = str(tmp_path / "test.mp3")
        bf.make_audio("hello world", filepath)
        assert os.path.exists(filepath)
        assert os.path.getsize(filepath) > 100

    def test_voice_parameter(self, tmp_path):
        filepath = str(tmp_path / "test_andrew.mp3")
        bf.make_audio("hello", filepath, voice=bf.VOICE_WORD)
        assert os.path.exists(filepath)

    def test_normalizes_text(self, tmp_path):
        filepath = str(tmp_path / "test_normalize.mp3")
        bf.make_audio("don’t stop", filepath)
        assert os.path.exists(filepath)


# ── Process note logic ──

class TestProcessNote:
    def _make_note(self, word, sentence="", image="", audio="", front_audio="", assoc=""):
        return {
            "noteId": 12345,
            "fields": {
                "Front": {"value": word},
                "Association": {"value": assoc},
                "Sentence": {"value": sentence},
                "Image_Prompt": {"value": image},
                "Audio": {"value": audio},
                "Front_Audio": {"value": front_audio},
            }
        }

    def test_skips_complete_note(self):
        note = self._make_note("test", sentence="A test.", image="<img>", audio="[sound:x]", front_audio="[sound:y]")
        word, status = bf.process_note(note)
        assert status == "skipped"

    @patch.object(bf, 'anki')
    @patch.object(bf, 'make_audio')
    @patch.object(bf, 'fetch_image', return_value=(True, '<attr>'))
    @patch.object(bf, 'llm_sentence_and_query', return_value=("A test sentence.", "test photo"))
    def test_fills_all_missing_fields(self, mock_llm, mock_img, mock_audio, mock_anki):
        note = self._make_note("test")
        word, status = bf.process_note(note)
        assert status == "done"
        mock_anki.assert_called_once()
        call_args = mock_anki.call_args
        fields = call_args.kwargs["note"]["fields"]
        assert "Sentence" in fields
        assert "Image_Prompt" in fields
        assert "Audio" in fields
        assert "Front_Audio" in fields

    @patch.object(bf, 'anki')
    @patch.object(bf, 'make_audio')
    @patch.object(bf, 'llm_sentence_and_query', return_value=("", "test photo"))
    @patch.object(bf, 'fetch_image', return_value=(False, ''))
    def test_handles_llm_failure(self, mock_img, mock_llm, mock_audio, mock_anki):
        note = self._make_note("test")
        word, status = bf.process_note(note)
        assert status == "done"
        call_args = mock_anki.call_args
        fields = call_args.kwargs["note"]["fields"]
        assert "Please add" in fields["Sentence"]

    def test_keeps_existing_sentence(self):
        note = self._make_note("test", sentence="Existing sentence.", image="<img>", audio="[sound:x]", front_audio="[sound:y]")
        word, status = bf.process_note(note)
        assert status == "skipped"


# ── gtts_helper (subprocess TTS) ──

class TestGttsHelper:
    def test_single_mode(self, tmp_path):
        import subprocess
        filepath = str(tmp_path / "test_single.mp3")
        result = subprocess.run(
            ["uv", "run", "python", "_gtts_helper.py", "hello world", filepath],
            capture_output=True, text=True, timeout=20,
            cwd="/Users/yeqilin/Workspace/Anki",
        )
        assert result.returncode == 0
        assert os.path.exists(filepath)

    def test_batch_mode(self, tmp_path):
        import subprocess
        f1 = str(tmp_path / "batch1.mp3")
        f2 = str(tmp_path / "batch2.mp3")
        items = json.dumps([
            {"text": "hello", "filepath": f1, "voice": "en-US-AndrewNeural"},
            {"text": "world", "filepath": f2, "voice": "en-US-AvaNeural"},
        ])
        result = subprocess.run(
            ["uv", "run", "python", "_gtts_helper.py", "--batch", items],
            capture_output=True, text=True, timeout=30,
            cwd="/Users/yeqilin/Workspace/Anki",
        )
        assert result.returncode == 0
        assert os.path.exists(f1)
        assert os.path.exists(f2)

    def test_voice_parameter(self, tmp_path):
        import subprocess
        filepath = str(tmp_path / "test_voice.mp3")
        result = subprocess.run(
            ["uv", "run", "python", "_gtts_helper.py", "test", filepath, "en-US-GuyNeural"],
            capture_output=True, text=True, timeout=20,
            cwd="/Users/yeqilin/Workspace/Anki",
        )
        assert result.returncode == 0
        assert os.path.exists(filepath)


# ── image_helper ──

class TestImageHelper:
    @patch('_image_helper.llm', return_value="test search query photo")
    def test_image_query_with_definition(self, mock_llm):
        from _image_helper import image_query
        result = image_query("audit", definition="examine carefully")
        assert len(result) > 3

    @patch('_image_helper.llm', return_value="")
    def test_image_query_fallback(self, mock_llm):
        from _image_helper import image_query
        result = image_query("audit", definition="examine")
        assert "audit" in result

    @patch('_image_helper.llm', return_value="")
    def test_image_query_no_definition(self, mock_llm):
        from _image_helper import image_query
        result = image_query("audit")
        assert "audit" in result
