"""詞義只選一次（2026-10-10）：沒有 Association 時先選詞義，搜圖與造句共用同一句。
事故：concrete 的搜圖選了「混凝土」、造句選了「具體類別」，圖和句子對不上。
不打網路：LLM 一律 patch；conftest 的 autouse 把選詞義預設成回空，這裡在模組層先拿原函式。"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import addon
import backfill_words
import core.llm as llm_mod

REAL_CORE_PICK = llm_mod.llm_pick_sense
REAL_ADDON_PICK = addon._llm._llm_pick_sense
SENSE = "a class that is not abstract in object-oriented programming"


class TestPrompt:
    def test_matches_addon(self):
        for w in ("concrete", "penguin", "nail down"):
            assert addon._llm._sense_prompt(w) == llm_mod.sense_prompt(w)

    def test_same_priority_rules_as_sentence(self):
        p = llm_mod.sense_prompt("penguin")
        assert "slang, nicknames and mascots do not count" in p
        assert "when in doubt use the everyday meaning" in p


class TestCleanSense:
    CASES = ["", None, "  ", '"a class that is not abstract"', "line one\nline two",
             " ".join(["word"] * 21), " ".join(["word"] * 20)]

    def test_matches_addon(self):
        for c in self.CASES:
            assert addon._llm._clean_sense(c) == llm_mod.clean_sense(c)
        assert addon._llm._MAX_SENSE_WORDS == llm_mod.MAX_SENSE_WORDS

    def test_strips_quotes(self):
        assert llm_mod.clean_sense('"a class that is not abstract"') == "a class that is not abstract"

    def test_rejects_multiline_and_too_long(self):
        assert llm_mod.clean_sense("line one\nline two") == ""
        assert llm_mod.clean_sense(" ".join(["word"] * 21)) == ""
        assert llm_mod.clean_sense(" ".join(["word"] * 20)) != ""

    def test_failure_gives_empty(self):
        with patch.object(llm_mod, "llm", lambda p, **kw: ""):
            assert REAL_CORE_PICK("concrete") == ""
        with patch.object(addon._llm, "_groq_chat", lambda p, **kw: ""):
            assert REAL_ADDON_PICK("concrete") == ""

    def test_addon_uses_light_pool(self):
        seen = {}
        with patch.object(addon._llm, "_groq_chat", lambda p, **kw: seen.update(kw) or SENSE):
            assert REAL_ADDON_PICK("concrete") == SENSE
        assert seen["task"] == "light"


# ── ⌘A ───────────────────────────────────────────────────────────────────────

class _Emit:
    def __init__(self): self.calls = []
    def emit(self, *a): self.calls.append(a)


def _cmd_a(association):
    w = addon._workers.Worker.__new__(addon._workers.Worker)
    w.word, w.association, w.media_dir = "concrete", association, "/tmp"
    w.step, w.finished, w.error = _Emit(), _Emit(), _Emit()
    seen = {"pick": 0}
    def pick(word):
        seen["pick"] += 1
        return SENSE
    w._pick_sense = pick
    w._fetch_image = lambda word, definition="": seen.update(image=definition) or ""
    w._llm_sentence = lambda word, association="", photo="": (
        seen.update(sentence=association) or ("The Logger class is concrete.", "Groq"))
    w._groq_translate = lambda word, s, **kw: "具體"
    w._groq_translate_sentence = lambda s, word="", **kw: "Logger 類別是具體的。"
    w._make_audio_batch = lambda items: None
    w.run()
    return w, seen


class TestCmdA:
    def test_no_association_one_sense_for_both(self):
        w, seen = _cmd_a("")
        assert seen["pick"] == 1
        assert seen["image"] == SENSE and seen["sentence"] == SENSE
        assert w.finished.calls[0][0]["association"] == ""      # 不寫回 Association 欄位

    def test_association_given_no_pick(self):
        _, seen = _cmd_a("具體類別")
        assert seen["pick"] == 0
        assert seen["image"] == "具體類別" and seen["sentence"] == "具體類別"


# ── ⌘S ───────────────────────────────────────────────────────────────────────

class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"error": null}'


def _note(sentence="", image="", association=""):
    vals = {"Front": "concrete", "Sentence": sentence, "Image_Prompt": image, "Audio": "",
            "Front_Audio": "[sound:concrete_word.mp3]", "Translation": "", "Sentence_CN": "",
            "Association": association}
    return {"noteId": 1, "fields": {k: {"value": v} for k, v in vals.items()}}


def _cmd_s(note):
    bw = addon._workers.BackfillWorker.__new__(addon._workers.BackfillWorker)
    bw.media_dir = "/tmp"
    bw._hit_limit = False; bw._stopped = False; bw.retry_after = 0; bw.limit_resets = {}
    bw.step, bw.card_done = _Emit(), _Emit()
    seen = {"pick": 0}
    def pick(word):
        seen["pick"] += 1
        return SENSE
    bw._w = SimpleNamespace(
        _pick_sense=pick,
        _fetch_image=lambda word, definition="": seen.update(image=definition) or '<img src="a.jpg">',
        _llm_sentence=lambda word, association="", photo="": (
            seen.update(sentence=association) or ("The Logger class is concrete.", "Groq")),
        _groq_translate=lambda word, s, **kw: "具體",
        _groq_translate_sentence=lambda s, strict=False, word="", **kw: "Logger 類別是具體的。",
        _make_audio_batch=lambda items: None,
    )
    with patch.object(addon._llm._dispatcher, "wall_secs", return_value=0), \
         patch.object(addon._workers.urllib.request, "urlopen", lambda req, timeout=None: _Resp()):
        bw._process_one(note)
    return seen


class TestCmdS:
    def test_empty_card_one_sense_for_both(self):
        seen = _cmd_s(_note())
        assert seen["pick"] == 1
        assert seen["image"] == SENSE and seen["sentence"] == SENSE

    def test_association_given_no_pick(self):
        seen = _cmd_s(_note(association="具體類別"))
        assert seen["pick"] == 0 and seen["sentence"] == "具體類別"

    def test_only_image_missing_no_pick(self):
        # 句子已經定了詞義 → 另外選詞義反而可能跟句子對不上
        seen = _cmd_s(_note(sentence="The Logger class is concrete."))
        assert seen["pick"] == 0 and seen["image"] == ""

    def test_only_sentence_missing_no_pick(self):
        seen = _cmd_s(_note(image='<img src="o.jpg" alt="Old.">'))
        assert seen["pick"] == 0 and seen["sentence"] == ""


# ── CLI backfill_words.py ─────────────────────────────────────────────────────

def _cli(note):
    seen = {"pick": 0}
    def pick(word):
        seen["pick"] += 1
        return SENSE
    with patch.object(backfill_words, "llm_pick_sense", pick), \
         patch.object(backfill_words, "llm_image_query",
                      lambda w, d="": seen.update(image=d) or "code screen"), \
         patch.object(backfill_words, "fetch_image", lambda w, p, search_query=None, **kw: (False, "", "", "")), \
         patch.object(backfill_words, "llm_sentence",
                      lambda w, association="", photo="": seen.update(sentence=association)
                      or "The Logger class is concrete."), \
         patch.object(backfill_words, "llm_translate", lambda w, s, sense="": "具體"), \
         patch.object(backfill_words, "make_audio", lambda *a, **k: None), \
         patch.object(backfill_words, "anki", MagicMock()):
        backfill_words.process_note(note)
    return seen


def _cli_note(**kw):
    n = _note(**kw)
    n["fields"]["Front_Audio"]["value"] = ""
    return n


class TestCli:
    def test_empty_card_one_sense_for_both(self):
        seen = _cli(_cli_note())
        assert seen["pick"] == 1
        assert seen["image"] == SENSE and seen["sentence"] == SENSE

    def test_association_given_no_pick(self):
        seen = _cli(_cli_note(association="具體類別"))
        assert seen["pick"] == 0 and seen["sentence"] == "具體類別"

    def test_only_image_missing_no_pick(self):
        seen = _cli(_cli_note(sentence="The Logger class is concrete."))
        assert seen["pick"] == 0 and seen["image"] == ""
