"""語音失敗不丟掉其他欄位（issue #2）。fake aqt 來自 conftest，不連網路、不跑 subprocess。

⌘A：照樣建卡，兩個語音欄位留空。⌘S：其他欄位照寫回，語音欄位不寫。
例句、圖、翻譯都保留；之後 ⌘S 會補空的語音欄位。
"""
import json
from types import SimpleNamespace
from unittest.mock import patch

import addon


class _Emit:
    def __init__(self): self.calls = []
    def emit(self, *a): self.calls.append(a)


def _worker(make_audio):
    w = addon._workers.Worker.__new__(addon._workers.Worker)
    w.word, w.association, w.media_dir = "ladder", "", "/tmp"
    w.step, w.finished, w.error = _Emit(), _Emit(), _Emit()
    w._fetch_image = lambda word, definition="": '<img src="a.jpg" alt="Ladder.">'
    w._llm_sentence = lambda word, association="", photo="": ("The ladder leans on the wall.", "Groq")
    w._groq_translate = lambda word, s, **kw: "梯子"
    w._groq_translate_sentence = lambda s, word="", **kw: "梯子靠在牆上。"
    w._make_audio_batch = make_audio
    return w


def _fail(items):
    raise RuntimeError("Audio generation failed. Check your network and retry.")


class TestAudioFailureKeepsCard:
    def test_card_is_still_added(self):
        w = _worker(_fail)
        w.run()
        assert w.error.calls == []
        assert len(w.finished.calls) == 1

    def test_other_fields_are_kept(self):
        w = _worker(_fail)
        w.run()
        data = w.finished.calls[0][0]
        assert data["sentence"] == "The ladder leans on the wall."
        assert "<img" in data["image_field"]
        assert data["translation"] == "梯子"
        assert data["sentence_cn"] == "梯子靠在牆上。"

    def test_both_audio_fields_left_empty(self):
        w = _worker(_fail)
        w.run()
        data = w.finished.calls[0][0]
        assert data["audio_filename"] == ""
        assert data["front_audio_filename"] == ""

    def test_audio_box_warns_with_reason(self):
        w = _worker(_fail)
        w.run()
        audio = [c for c in w.step.calls if c[0] == "audio"]
        assert audio == [("audio", "warn", "audio failed")]

    def test_audio_ok_fills_both_fields(self):
        w = _worker(lambda items: None)
        w.run()
        data = w.finished.calls[0][0]
        assert data["audio_filename"] == "ladder_tts.mp3"
        assert data["front_audio_filename"] == "ladder_word.mp3"


class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"error": null}'


def _run_s(make_audio):
    """⌘S 補一張全空的卡；回傳寫回 Anki 的欄位與 step 訊號。"""
    note = {"noteId": 1, "fields": {k: {"value": ""} for k in (
        "Front", "Sentence", "Image_Prompt", "Audio", "Front_Audio",
        "Translation", "Sentence_CN", "Association")}}
    note["fields"]["Front"]["value"] = "ladder"
    bw = addon._workers.BackfillWorker.__new__(addon._workers.BackfillWorker)
    bw.media_dir = "/tmp"
    bw._hit_limit = False; bw._stopped = False; bw.retry_after = 0; bw.limit_resets = {}
    bw.step, bw.card_done = _Emit(), _Emit()
    bw._w = SimpleNamespace(
        _pick_sense=lambda word: "",
        _fetch_image=lambda word, definition="": '<img src="a.jpg" alt="Ladder.">',
        _llm_sentence=lambda word, association="", photo="": ("The ladder leans on the wall.", "Groq"),
        _groq_translate=lambda word, s, **kw: "梯子",
        _groq_translate_sentence=lambda s, word="", **kw: "梯子靠在牆上。",
        _make_audio_batch=make_audio,
    )
    sent = {}
    def fake_urlopen(req, timeout=None):
        sent.update(json.loads(req.data.decode())["params"]["note"]["fields"])
        return _Resp()
    with patch.object(addon._llm._dispatcher, "wall_secs", return_value=0), \
         patch.object(addon._workers.urllib.request, "urlopen", fake_urlopen):
        bw._process_one(note)
    return sent, bw.step.calls


class TestCmdSAudioFailureKeepsFields:
    def test_other_fields_are_written(self):
        sent, _ = _run_s(_fail)
        assert sent["Sentence"] == "The ladder leans on the wall."
        assert "<img" in sent["Image_Prompt"]
        assert sent["Translation"] == "梯子"
        assert sent["Sentence_CN"] == "梯子靠在牆上。"

    def test_audio_fields_not_written(self):
        sent, _ = _run_s(_fail)
        assert "Audio" not in sent and "Front_Audio" not in sent

    def test_audio_box_warns_with_reason(self):
        _, steps = _run_s(_fail)
        assert (1, "audio", "warn", "audio failed") in steps

    def test_audio_ok_writes_both_fields(self):
        sent, _ = _run_s(lambda items: None)
        assert sent["Audio"] == "[sound:ladder_tts.mp3]"
        assert sent["Front_Audio"] == "[sound:ladder_word.mp3]"


class TestCmdSRefillsEmptyAudio:
    """⌘S 判斷要不要補句音的純函式：⌘A 留空的 Audio 會被補。"""
    def test_empty_sentence_audio_is_refilled(self):
        assert addon._text._need_sentence_audio("", "The ladder leans on the wall.", False)


# ── CLI（backfill_words.py／add_word.py 共用 core.tts.try_make_audio）─────────

import core.tts


class TestTryMakeAudio:
    def test_failure_returns_false(self):
        with patch.object(core.tts, "make_audio", side_effect=RuntimeError("429")):
            assert core.tts.try_make_audio("hi", "/tmp/x.mp3") is False

    def test_success_returns_true(self):
        with patch.object(core.tts, "make_audio", return_value=None):
            assert core.tts.try_make_audio("hi", "/tmp/x.mp3") is True


def _run_cli(audio_ok):
    import backfill_words
    note = {"noteId": 1, "fields": {k: {"value": ""} for k in (
        "Front", "Sentence", "Image_Prompt", "Audio", "Front_Audio",
        "Translation", "Sentence_CN", "Association")}}
    note["fields"]["Front"]["value"] = "ladder"
    sent = {}
    def fake_anki(action, **kw):
        if action == "updateNoteFields":
            sent.update(kw["note"]["fields"])
    with patch.object(backfill_words, "anki", fake_anki), \
         patch.object(backfill_words, "llm_pick_sense", return_value=""), \
         patch.object(backfill_words, "llm_image_query", return_value="ladder"), \
         patch.object(backfill_words, "_do_image", return_value=("ladder_img.jpg", True, "", "Ladder.", "pexels:1")), \
         patch.object(backfill_words, "llm_sentence", return_value="The ladder leans on the wall."), \
         patch.object(backfill_words, "llm_translate", return_value="梯子"), \
         patch.object(backfill_words, "try_make_audio", return_value=audio_ok):
        backfill_words.process_note(note)
    return sent


class TestCliBackfillAudioFailure:
    def test_other_fields_are_written(self):
        sent = _run_cli(audio_ok=False)
        assert sent["Sentence"] == "The ladder leans on the wall."
        assert "<img" in sent["Image_Prompt"]
        assert sent["Translation"] == "梯子"

    def test_audio_fields_not_written(self):
        sent = _run_cli(audio_ok=False)
        assert "Audio" not in sent and "Front_Audio" not in sent

    def test_audio_ok_writes_both_fields(self):
        sent = _run_cli(audio_ok=True)
        assert sent["Audio"] == "[sound:ladder_tts.mp3]"
        assert sent["Front_Audio"] == "[sound:ladder_word.mp3]"
