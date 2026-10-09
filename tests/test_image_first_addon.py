"""addon image-first flow (fake aqt from conftest). No network/LLM/subprocess."""
import json
from types import SimpleNamespace
from unittest.mock import patch
import addon
import core.llm as llm_mod
import core.text as text_mod


class TestKeepInSync:
    def test_prompt_matches_core(self):
        for args in (("ladder", "", ""), ("thread", "sewing", ""), ("ladder", "", 'A "red" ladder.')):
            assert addon._llm._sentence_prompt(*args) == (
                llm_mod._sentence_instructions(*args)
                + "\n\nOutput only the sentence. No explanation, no quotes.")

    def test_image_helpers_match_core(self):
        cases = [("", "", ""), ("A cat.", "<div>a</div>", ""), ('A "big" dog & <cat>\non grass', "", "wikimedia:12"),
                 ("", "", "pixabay:9"), ("x", "<div>b</div>", 'q"uote:1')]
        for desc, attr, src in cases:
            h = addon._images._image_html("f.jpg", desc, attr, src)
            assert h == text_mod.image_html("f.jpg", desc, attr, src)
            assert addon._images._image_alt(h) == text_mod.image_alt(h)
            assert addon._images._image_source(h) == text_mod.image_source(h)

    def test_existing_image_without_alt_gives_no_photo(self):
        assert addon._images._image_alt('<img src="old.jpg"><div>Photo by X</div>') == ""

    def test_legacy_image_source_is_pexels(self):
        assert addon._images._image_source('<img src="old.jpg">') == "pexels:"
        assert addon._images._image_source("") == ""
        assert addon._images._image_source('<img src="n.jpg" alt="a" data-source="openverse:ab-1">') == "openverse:ab-1"


class TestFetchImageParsesAlt:
    def test_alt_and_attribution(self, tmp_path):
        w = addon._workers.Worker.__new__(addon._workers.Worker); w.media_dir = str(tmp_path)
        out = SimpleNamespace(returncode=0, stdout="ALT: Ladder on a wall.\nATTRIBUTION: <div>P</div>\n")
        with patch.object(addon._workers.subprocess, "run", return_value=out) as run:
            html = w._fetch_image("ladder", definition="climb")
        assert addon._images._image_alt(html) == "Ladder on a wall."
        assert html.endswith("<div>P</div>")
        assert "--sentence" not in run.call_args.args[0]

    def test_source_line_parsed_into_data_source(self, tmp_path):
        w = addon._workers.Worker.__new__(addon._workers.Worker); w.media_dir = str(tmp_path)
        out = SimpleNamespace(returncode=0,
                              stdout="ALT: Ladder on a wall.\nATTRIBUTION: <div>P</div>\nSOURCE: wikimedia:555\n")
        with patch.object(addon._workers.subprocess, "run", return_value=out):
            html = w._fetch_image("ladder")
        assert addon._images._image_source(html) == "wikimedia:555"
        assert addon._images._image_alt(html) == "Ladder on a wall."

    def test_missing_source_line_gives_legacy_pexels(self, tmp_path):
        w = addon._workers.Worker.__new__(addon._workers.Worker); w.media_dir = str(tmp_path)
        out = SimpleNamespace(returncode=0, stdout="ALT: x\n")
        with patch.object(addon._workers.subprocess, "run", return_value=out):
            html = w._fetch_image("ladder")
        assert 'data-source' not in html and addon._images._image_source(html) == "pexels:"


class _Emit:
    def __init__(self): self.calls = []
    def emit(self, *a): self.calls.append(a)


def _worker(image_html, sentence):
    w = addon._workers.Worker.__new__(addon._workers.Worker)
    w.word, w.association, w.media_dir = "ladder", "", "/tmp"
    w.step, w.finished, w.error = _Emit(), _Emit(), _Emit()
    seen = {}
    w._fetch_image = lambda word, definition="": image_html
    def fake_sentence(word, association="", photo=""):
        seen["photo"] = photo
        return (sentence, "Groq") if sentence else ("", "failed")
    w._llm_sentence = fake_sentence
    w._groq_translate = lambda word, s, **kw: "梯子"
    w._groq_translate_sentence = lambda s, word="", **kw: "梯子靠在牆上。"
    w._make_audio_batch = lambda items: None
    return w, seen


class TestCmdAOrder:
    def test_sentence_gets_photo(self):
        w, seen = _worker('<img src="a.jpg" alt="Ladder on a wall.">', "The ladder leans on the wall.")
        w.run()
        assert seen["photo"] == "Ladder on a wall."
        data = w.finished.calls[0][0]
        assert data["sentence"] == "The ladder leans on the wall."
        assert data["sentence_cn"] == "梯子靠在牆上。"

    def test_no_image_still_makes_sentence(self):
        w, seen = _worker("", "The ladder leans on the wall.")
        w.run()
        assert seen["photo"] == ""
        assert w.finished.calls[0][0]["sentence"] == "The ladder leans on the wall."

    def test_sentence_fail_keeps_image_skips_translation(self):
        w, _ = _worker('<img src="a.jpg" alt="Ladder.">', "")
        w.run()
        data = w.finished.calls[0][0]
        assert "<img" in data["image_field"]
        assert data["translation"] == "" and data["sentence_cn"] == ""
        assert data["audio_filename"] == ""


# ── ⌘S ──────────────────────────────────────────────────────────────────────

class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"error": null}'


def _note(sentence="", image="", **extra):
    vals = {"Front": "ladder", "Sentence": sentence, "Image_Prompt": image, "Audio": "",
            "Front_Audio": "[sound:ladder_word.mp3]", "Translation": "", "Sentence_CN": "",
            "Association": ""}
    vals.update(extra)
    return {"noteId": 1, "fields": {k: {"value": v} for k, v in vals.items()}}


def _run_s(note, image_html, sentence):
    bw = addon._workers.BackfillWorker.__new__(addon._workers.BackfillWorker)
    bw.media_dir = "/tmp"
    bw._hit_limit = False; bw._stopped = False; bw.retry_after = 0; bw.limit_resets = {}
    bw.step, bw.card_done = _Emit(), _Emit()
    log = []
    w = SimpleNamespace()
    def fetch(word, definition=""):
        log.append("image"); return image_html
    def llm_sentence(word, association="", photo=""):
        log.append(("sentence", photo))
        return (sentence, "Groq") if sentence else ("", "failed")
    w._fetch_image = fetch
    w._llm_sentence = llm_sentence
    w._groq_translate = lambda word, s, **kw: "梯子"
    w._groq_translate_sentence = lambda s, strict=False, word="", **kw: "梯子靠在牆上。"
    w._make_audio_batch = lambda items: None
    bw._w = w
    sent = {}
    def fake_urlopen(req, timeout=None):
        sent.update(json.loads(req.data.decode())["params"]["note"]["fields"])
        return _Resp()
    with patch.object(addon._llm._dispatcher, "wall_secs", return_value=0), \
         patch.object(addon._workers.urllib.request, "urlopen", fake_urlopen):
        bw._process_one(note)
    return log, sent


class TestCmdSOrder:
    def test_empty_card_image_before_sentence(self):
        img = '<img src="a.jpg" alt="Ladder on a wall.">'
        log, sent = _run_s(_note(), img, "The ladder leans on the wall.")
        assert log == ["image", ("sentence", "Ladder on a wall.")]
        assert 'alt="Ladder on a wall."' in sent["Image_Prompt"]
        assert sent["Sentence"] == "The ladder leans on the wall."

    def test_real_sentence_no_image(self):
        log, sent = _run_s(_note(sentence="The ladder is tall."), '<img src="a.jpg" alt="X.">', "Other.")
        assert log == ["image"]
        assert "Sentence" not in sent
        assert "Image_Prompt" in sent

    def test_existing_image_with_alt_empty_sentence(self):
        log, sent = _run_s(_note(image='<img src="o.jpg" alt="Old.">'), "", "The ladder leans on the wall.")
        assert log == [("sentence", "Old.")]
        assert "Image_Prompt" not in sent

    def test_sentence_fails_keeps_image_skips_downstream(self):
        log, sent = _run_s(_note(), '<img src="a.jpg" alt="Ladder.">', "")
        assert "Image_Prompt" in sent
        for k in ("Translation", "Sentence_CN", "Audio"):
            assert k not in sent


def test_image_query_prompt_matches_core():
    import addon
    import core.llm
    for w, d in [("penguin", ""), ("idempotent", "重複執行結果相同")]:
        assert addon._llm._image_query_prompt(w, d) == core.llm.image_query_prompt(w, d)


def test_fetch_image_passes_query_to_helper(monkeypatch):
    import addon
    calls = {}

    class Done:
        returncode, stdout, stderr = 1, "", ""
    monkeypatch.setattr(addon._llm, "_groq_chat", lambda p, **kw: "penguin on ice")
    monkeypatch.setattr(addon._workers.subprocess, "run",
                        lambda cmd, **kw: calls.setdefault("cmd", cmd) and Done())
    w = addon._workers.Worker.__new__(addon._workers.Worker)
    w.media_dir = "/tmp"
    w._fetch_image("penguin")
    cmd = calls["cmd"]
    assert cmd[cmd.index("--query") + 1] == "penguin on ice"
