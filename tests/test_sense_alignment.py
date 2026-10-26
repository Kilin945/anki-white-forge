"""例句和翻譯對齊同一個詞義（2026-10-10）。
事故：waterfalls 提示是「瀑布式開發流程」，例句卻寫成不自然的「outlined the waterfalls on the
strategy document」，整句翻譯寫「瀑布圖」、單字翻譯寫「瀑布模型」——造完句沒人檢查意思，兩個翻譯
也各自猜詞義。不打網路：LLM 一律 patch。"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import addon
import backfill_words
import core.llm as llm_mod

SENSE = "瀑布式開發流程"
GOOD = "Our team used the waterfall model for the project."
BAD = "He outlined the waterfalls on the strategy document."


# ── prompt 兩份逐字相同 ───────────────────────────────────────────────────────

class TestKeepInSync:
    def test_sense_check_prompt(self):
        a = addon._llm._sense_check_prompt("waterfall", SENSE, BAD)
        assert a == llm_mod.sense_check_prompt("waterfall", SENSE, BAD)
        assert SENSE in a and BAD in a and "YES or NO" in a

    def test_sentence_prompt_with_previous(self):
        args = ("waterfall", SENSE, "")
        a = addon._llm._sentence_prompt(*args, previous=BAD)
        c = (llm_mod._sentence_instructions(*args, previous=BAD)
             + "\n\nOutput only the sentence. No explanation, no quotes.")
        assert a == c
        assert BAD in a

    def test_translation_sense_template(self):
        assert addon._llm._TRANSLATION_SENSE_TEMPLATE == llm_mod.TRANSLATION_SENSE_TEMPLATE


class TestSenseVerdict:
    def test_only_a_clear_no_fails(self):
        for reply, ok in [("NO", False), ("no.", False), ("YES", True), ("", True), ("maybe", True)]:
            assert llm_mod.sense_fits_reply(reply) is ok
            assert addon._llm._sense_fits_reply(reply) is ok


# ── core 造句 ─────────────────────────────────────────────────────────────────

def _core_sentence(association, verdict, replies):
    prompts = []
    replies = iter(replies)
    def fake_llm(prompt, **kw):
        prompts.append(prompt)
        if "Answer only YES or NO" in prompt:
            return verdict
        return next(replies)
    with patch.object(llm_mod, "llm", fake_llm):
        out = llm_mod.llm_sentence("waterfall", association)
    return out, prompts


class TestCoreSentence:
    def test_mismatch_retries_with_previous(self):
        out, prompts = _core_sentence(SENSE, "NO", [BAD.replace("waterfalls", "waterfall"), GOOD])
        assert out == GOOD
        assert "did not use" in prompts[-1]

    def test_fits_no_retry(self):
        out, prompts = _core_sentence(SENSE, "YES", [GOOD])
        assert out == GOOD
        assert len(prompts) == 2                     # 造句 + 檢查

    def test_no_hint_no_check(self):
        out, prompts = _core_sentence("", "NO", [GOOD])
        assert out == GOOD and len(prompts) == 1


# ── addon 造句 ────────────────────────────────────────────────────────────────

def _addon_sentence(association, verdict, replies, monkeypatch):
    prompts = []
    replies = iter(replies)
    def fake_chat(prompt, **kw):
        prompts.append((prompt, kw.get("task")))
        if "Answer only YES or NO" in prompt:
            return verdict
        return next(replies)
    monkeypatch.setattr(addon._llm, "_groq_chat", fake_chat)
    w = addon._workers.Worker.__new__(addon._workers.Worker)
    return w._llm_sentence("waterfall", association), prompts


class TestAddonSentence:
    def test_mismatch_retries_with_previous(self, monkeypatch):
        (out, _), prompts = _addon_sentence(SENSE, "NO",
                                            [BAD.replace("waterfalls", "waterfall"), GOOD], monkeypatch)
        assert out == GOOD
        assert "did not use" in prompts[-1][0]
        assert [t for p, t in prompts if "Answer only YES or NO" in p] == ["light"]

    def test_fits_no_retry(self, monkeypatch):
        (out, _), prompts = _addon_sentence(SENSE, "YES", [GOOD], monkeypatch)
        assert out == GOOD and len(prompts) == 2

    def test_no_hint_no_check(self, monkeypatch):
        (out, _), prompts = _addon_sentence("", "NO", [GOOD], monkeypatch)
        assert out == GOOD and len(prompts) == 1


# ── 翻譯帶詞義 ────────────────────────────────────────────────────────────────

class TestTranslationGetsSense:
    def test_core_word_and_sentence(self):
        seen = []
        with patch.object(llm_mod, "llm", lambda p, **kw: seen.append(p) or "瀑布模型"):
            llm_mod.llm_translate("waterfall", GOOD, sense=SENSE)
            llm_mod.llm_translate_sentence(GOOD, word="waterfall", sense=SENSE)
        assert all(SENSE in p for p in seen) and len(seen) == 2

    def test_core_no_sense_no_line(self):
        seen = []
        with patch.object(llm_mod, "llm", lambda p, **kw: seen.append(p) or "瀑布"):
            llm_mod.llm_translate("waterfall", GOOD)
        assert "here means" not in seen[0]

    def test_addon_word_and_sentence(self, monkeypatch):
        seen = []
        monkeypatch.setattr(addon._llm, "_groq_chat", lambda p, **kw: seen.append(p) or "瀑布模型")
        w = addon._workers.Worker.__new__(addon._workers.Worker)
        w._groq_translate("waterfall", GOOD, sense=SENSE)
        w._groq_translate_sentence(GOOD, word="waterfall", sense=SENSE)
        assert all(SENSE in p for p in seen) and len(seen) == 2


class TestSentenceCnSenseBeforeSentence:
    """詞義要放在句子前面（2026-10-22）。事故：詞義接在 `Sentence: "…"` 後面，
    gemini-3.5-flash-lite 把它當成句子一起翻，Sentence_CN 多出「這裡的 departure 意思是：…」
    （舊 prompt 12 次漏 10 次，詞義移到前面 0 次）。"""

    def test_core_and_addon_prompt_match(self):
        for word, sense in (("waterfall", SENSE), ("", ""), ("", SENSE), ("waterfall", "")):
            assert (llm_mod.sentence_cn_prompt(GOOD, word=word, sense=sense)
                    == addon._llm._sentence_cn_prompt(GOOD, word=word, sense=sense))

    def test_sense_before_sentence(self):
        for p in (llm_mod.sentence_cn_prompt(GOOD, word="waterfall", sense=SENSE),
                  addon._llm._sentence_cn_prompt(GOOD, word="waterfall", sense=SENSE)):
            assert p.index(SENSE) < p.index("Sentence:")
            assert p.endswith(f'Sentence: "{GOOD}"')

    def test_callers_send_that_prompt(self, monkeypatch):
        seen = []
        with patch.object(llm_mod, "llm", lambda p, **kw: seen.append(p) or "團隊用瀑布模型。"):
            llm_mod.llm_translate_sentence(GOOD, word="waterfall", sense=SENSE)
        monkeypatch.setattr(addon._llm, "_groq_chat", lambda p, **kw: seen.append(p) or "團隊用瀑布模型。")
        w = addon._workers.Worker.__new__(addon._workers.Worker)
        w._groq_translate_sentence(GOOD, word="waterfall", sense=SENSE)
        assert seen == [llm_mod.sentence_cn_prompt(GOOD, word="waterfall", sense=SENSE)] * 2


# 整句翻譯多寫了單字解釋就退回（2026-10-22）。英文原句本身在講「意思」「這個字」時是正常翻譯，要放過
LEAKED = [
    ("The passengers wait in the terminal for their departure.",
     "「乘客們在航廈裡等待出發。」這裡的 departure 意思是：「從某個地方或情況離開。」"),
    ("The forest began to regenerate, sprouting new trees quickly.",
     "這座森林開始復育，迅速長出新樹。這裡的「復育」一詞意指：自然地將某物恢復到原本或健康的狀態。"),
    ("The vintage snacks on the shelf go stale for shoppers.",
     "「架上的懷舊零食對消費者而言變得不再新穎。」這裡的「變質／失去新鮮感」意思是：「隨著時間推移而過時或變得比較沒吸引力。」"),
]
FAITHFUL = [
    ("The variable `user` refers to the logged-in person.", "變數 `user` 指的是已登入的使用者。"),
    ("The dictionary entry shows how to pronounce reject, which means to refuse a request.",
     "詞典條目顯示了 reject 的發音，意思是拒絕請求。"),
    ("A hand circles the word concerns on the glass board, highlighting worries.",
     "一只手在玻璃白板上的 concerns 一詞畫圈，突顯出心中的憂慮。"),
    ("The passengers wait in the terminal for their departure.", "乘客在航站樓等待他們的離港。"),
    # 沒冒號的「是指」「這裡的」「含義」是正常譯文（code review 實測會被第一版誤擋）
    ("This is a landmark building.", "這是指標性的建築。"),
    ("This is a guide for new users.", "這是指南。"),
    ("The locals are friendly.", "這裡的居民很友善。"),
    ("He could not understand the point of the remark.", "他無法理解這句話的含義。"),
]


class TestExplainsWord:
    def test_leaked_rejected_faithful_kept(self):
        for check in (llm_mod.explains_word, addon._text._explains_word):
            assert all(check(cn, en) for en, cn in LEAKED)
            assert not any(check(cn, en) for en, cn in FAITHFUL)

    def test_core_and_addon_markers_match(self):
        assert llm_mod.EXPLAIN_MARKERS == addon._text._EXPLAIN_MARKERS

    def test_core_returns_empty(self):
        en, cn = LEAKED[0]
        with patch.object(llm_mod, "llm", lambda p, **kw: cn):
            assert llm_mod.llm_translate_sentence(en, word="departure") == ""

    def test_addon_returns_empty_with_reason(self, monkeypatch):
        en, cn = LEAKED[0]
        monkeypatch.setattr(addon._llm, "_groq_chat", lambda p, **kw: cn)
        w = addon._workers.Worker.__new__(addon._workers.Worker)
        reasons = {}
        assert w._groq_translate_sentence(en, word="departure", reasons=reasons) == ""
        assert reasons == {"sentence_cn": "explains the word"}


# ── 三條路徑都把詞義傳給翻譯 ───────────────────────────────────────────────────

class _Emit:
    def __init__(self): self.calls = []
    def emit(self, *a): self.calls.append(a)


def test_cmd_a_passes_hint_to_translations():
    w = addon._workers.Worker.__new__(addon._workers.Worker)
    w.word, w.association, w.media_dir = "waterfall", SENSE, "/tmp"
    w.step, w.finished, w.error = _Emit(), _Emit(), _Emit()
    seen = {}
    w._fetch_image = lambda word, definition="": '<img src="a.jpg">'
    w._llm_sentence = lambda word, association="", photo="": (GOOD, "Groq")
    w._groq_translate = lambda word, s, sense="", **kw: seen.update(word=sense) or "瀑布模型"
    w._groq_translate_sentence = lambda s, word="", sense="", **kw: seen.update(cn=sense) or "團隊用瀑布模型。"
    w._make_audio_batch = lambda items: None
    w.run()
    assert seen == {"word": SENSE, "cn": SENSE}


class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"error": null}'


def test_cmd_s_passes_hint_to_translations():
    vals = {"Front": "waterfall", "Sentence": "", "Image_Prompt": '<img src="o.jpg">', "Audio": "",
            "Front_Audio": "[sound:w.mp3]", "Translation": "", "Sentence_CN": "", "Association": SENSE}
    note = {"noteId": 1, "fields": {k: {"value": v} for k, v in vals.items()}}
    bw = addon._workers.BackfillWorker.__new__(addon._workers.BackfillWorker)
    bw.media_dir = "/tmp"
    bw._hit_limit = False; bw._stopped = False; bw.retry_after = 0; bw.limit_resets = {}
    bw.step, bw.card_done = _Emit(), _Emit()
    seen = {}
    bw._w = SimpleNamespace(
        _pick_sense=lambda word: "",
        _fetch_image=lambda word, definition="": '<img src="a.jpg">',
        _llm_sentence=lambda word, association="", photo="": (GOOD, "Groq"),
        _groq_translate=lambda word, s, sense="", **kw: seen.update(word=sense) or "瀑布模型",
        _groq_translate_sentence=lambda s, strict=False, word="", sense="", **kw: seen.update(cn=sense) or "團隊用瀑布模型。",
        _make_audio_batch=lambda items: None,
    )
    with patch.object(addon._llm._dispatcher, "wall_secs", return_value=0), \
         patch.object(addon._workers.urllib.request, "urlopen", lambda req, timeout=None: _Resp()):
        bw._process_one(note)
    assert seen == {"word": SENSE, "cn": SENSE}


def test_cli_passes_hint_to_translation():
    vals = {"Front": "waterfall", "Sentence": "", "Image_Prompt": '<img src="o.jpg">', "Audio": "",
            "Front_Audio": "", "Translation": "", "Sentence_CN": "", "Association": SENSE}
    note = {"noteId": 1, "fields": {k: {"value": v} for k, v in vals.items()}}
    seen = {}
    with patch.object(backfill_words, "llm_sentence", lambda w, association="", photo="": GOOD), \
         patch.object(backfill_words, "llm_translate",
                      lambda w, s, sense="": seen.update(word=sense) or "瀑布模型"), \
         patch.object(backfill_words, "try_make_audio", lambda *a, **k: True), \
         patch.object(backfill_words, "anki", MagicMock()):
        backfill_words.process_note(note)
    assert seen == {"word": SENSE}
