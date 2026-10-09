"""Image-first sentence flow: core helpers + prompt wiring (no real network / LLM)."""
from unittest.mock import patch, MagicMock
import core.llm as llm_mod
import core.image as image_mod
from core.text import image_alt, image_html, has_image


class TestImageHtml:
    def test_alt_roundtrip(self):
        html = image_html("cat_img_1.jpg", "A cat on a sofa.", "<div>Photo by X</div>")
        assert html.startswith('<img src="cat_img_1.jpg" alt="A cat on a sofa.">')
        assert html.endswith("<div>Photo by X</div>")
        assert image_alt(html) == "A cat on a sofa."
        assert has_image(html)

    def test_roundtrip_escapes_quotes_and_newlines(self):
        desc = 'A "big" dog & <cat>\non   grass'
        html = image_html("d.jpg", desc)
        assert '"big"' not in html            # quotes escaped inside the attribute
        assert image_alt(html) == 'A "big" dog & <cat> on grass'

    def test_no_description_no_alt_attr(self):
        assert image_html("d.jpg") == '<img src="d.jpg">'

    def test_source_attribute_roundtrip(self):
        from core.text import image_source
        h = image_html("c.jpg", "A cat.", "<div>P</div>", source="wikimedia:123")
        assert h.startswith('<img src="c.jpg" alt="A cat." data-source="wikimedia:123">')
        assert image_source(h) == "wikimedia:123"
        assert image_alt(h) == "A cat."

    def test_source_without_description(self):
        from core.text import image_source
        h = image_html("c.jpg", source="pixabay:9")
        assert h == '<img src="c.jpg" data-source="pixabay:9">'
        assert image_source(h) == "pixabay:9"

    def test_legacy_image_is_pexels_unknown_id(self):
        from core.text import image_source
        assert image_source('<img src="old.jpg"><div>Photo by X on Pexels</div>') == "pexels:"
        assert image_source('<img src="old.jpg" alt="desc">') == "pexels:"

    def test_no_image_no_source(self):
        from core.text import image_source
        assert image_source("") == "" and image_source(None) == "" and image_source("<div>x</div>") == ""

    def test_image_alt_missing(self):
        assert image_alt('<img src="old.jpg">') == ""
        assert image_alt("") == ""
        assert image_alt(None) == ""


class TestFetchImageDescription:
    def test_pexels_alt_and_source_returned(self, tmp_path, monkeypatch):
        monkeypatch.setattr(image_mod, "_load_pexels_key", lambda: "k")
        monkeypatch.setattr(image_mod, "SOURCES", [("pexels", image_mod._search_pexels)])
        search = MagicMock(status_code=200)
        search.json.return_value = {"photos": [{"id": 1, 
            "src": {"large": "http://x/1.jpg"}, "alt": "Ladder against a wall.",
            "photographer": "P", "url": "http://pexels/1"}]}
        img = MagicMock(status_code=200, content=b"x" * 6000)
        with patch.object(image_mod.requests, "get", side_effect=[search, img]):
            ok, attr, desc, tag = image_mod.fetch_image("ladder", str(tmp_path / "a.jpg"), "ladder", rejects=[])
        assert ok and "Pexels" in attr and desc == "Ladder against a wall." and tag == "pexels:1"

    def test_failure_returns_four_tuple(self, tmp_path, monkeypatch):
        monkeypatch.setattr(image_mod, "SOURCES", [("none", lambda q: [])])
        assert image_mod.fetch_image("x", str(tmp_path / "a.jpg"), "x", rejects=[]) == (False, "", "", "")


def _capture(fn, *args, **kw):
    seen = {}
    def fake_llm(prompt, **k):
        seen["prompt"] = prompt
        return "The ladder leans against the garage wall."
    with patch.object(llm_mod, "llm", fake_llm):
        fn(*args, **kw)
    return seen["prompt"]


class TestPhotoInSentencePrompt:
    def test_photo_block_present(self):
        p = _capture(llm_mod.llm_sentence, "ladder", "", photo="Ladder against a wall.")
        assert "Photo description: \"Ladder against a wall.\"" in p
        assert "Never change the meaning or the setting to fit the photo" in p

    def test_no_photo_no_block(self):
        p = _capture(llm_mod.llm_sentence, "ladder")
        assert "Photo description" not in p

    def test_photo_block_before_output_line(self):
        p = _capture(llm_mod.llm_sentence, "ladder", "", photo="A ladder.")
        assert p.index("Photo description") < p.index("Output only the sentence")


class TestImageQueryNoSentence:
    def test_uses_sense_priority_and_hint(self):
        p = _capture(llm_mod.llm_image_query, "thread", definition="sewing")
        assert "sewing" in p
        assert "software-engineering" in p
        assert "used in the sentence" not in p
        assert "computer or tech scene" in p

    def test_no_sentence_parameter(self):
        import inspect
        assert "sentence" not in inspect.signature(llm_mod.llm_image_query).parameters

    def test_llm_sentence_and_query_removed(self):
        assert not hasattr(llm_mod, "llm_sentence_and_query")


import backfill_words


def _note(sentence="", image="", audio="", front_audio="[sound:w.mp3]", translation="", assoc=""):
    return {"noteId": 1, "fields": {
        "Front": {"value": "ladder"}, "Sentence": {"value": sentence},
        "Image_Prompt": {"value": image}, "Audio": {"value": audio},
        "Front_Audio": {"value": front_audio}, "Translation": {"value": translation},
        "Association": {"value": assoc}}}


class TestBackfillWordsOrder:
    def _run(self, note, fetch=(True, "", "Ladder against a wall.", "pexels:1"), sentence="The ladder leans on the wall."):
        calls = {}
        def fake_sentence(word, association="", photo=""):
            calls["photo"] = photo
            return sentence
        def fake_fetch(word, path, search_query=None):
            calls["query"] = search_query
            return fetch
        with patch.object(backfill_words, "llm_sentence", fake_sentence), \
             patch.object(backfill_words, "llm_image_query", lambda w, d="": "ladder wall"), \
             patch.object(backfill_words, "fetch_image", fake_fetch), \
             patch.object(backfill_words, "llm_translate", lambda w, s, sense="": "梯子"), \
             patch.object(backfill_words, "make_audio", lambda *a, **k: None), \
             patch.object(backfill_words, "anki", MagicMock()) as anki:
            backfill_words.process_note(note)
        fields = anki.call_args.kwargs["note"]["fields"] if anki.called else {}
        return calls, fields

    def test_sentence_uses_new_photo_description(self):
        calls, fields = self._run(_note())
        assert calls["photo"] == "Ladder against a wall."
        assert 'alt="Ladder against a wall."' in fields["Image_Prompt"]
        assert 'data-source="pexels:1"' in fields["Image_Prompt"]

    def test_image_fail_still_generates_sentence(self):
        calls, fields = self._run(_note(), fetch=(False, "", "", ""))
        assert calls["photo"] == ""
        assert fields["Sentence"] == "The ladder leans on the wall."

    def test_existing_sentence_not_rewritten_when_image_missing(self):
        calls, fields = self._run(_note(sentence="My ladder is tall.", audio="[sound:a.mp3]", translation="梯子"))
        assert "photo" not in calls
        assert "Sentence" not in fields
        assert "<img" in fields["Image_Prompt"]

    def test_existing_image_alt_feeds_sentence(self):
        calls, fields = self._run(_note(image='<img src="o.jpg" alt="Old ladder photo.">'))
        assert calls["photo"] == "Old ladder photo."
        assert "query" not in calls                    # no new image search
        assert "Image_Prompt" not in fields

    def test_sentence_fail_still_writes_image(self):
        calls, fields = self._run(_note(), sentence="")
        assert "<img" in fields["Image_Prompt"]
        assert "Translation" not in fields
        assert "Audio" not in fields
