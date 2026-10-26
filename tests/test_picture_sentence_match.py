"""圖和例句要對上（2026-10-26）：圖是退回日常義找到的，例句和翻譯也改用日常義；
卡上已有例句、這次不重寫時，不准退回日常義（退回去的圖一定對不上那句）。

事故：concrete 技術義找不到圖 → 退回日常義放混凝土牆，例句卻照技術義寫成
"The developer implemented a concrete class…"，圖文完全對不上。"""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import addon
import core.image as img
import core.picture as pic


@pytest.fixture
def fast_download(monkeypatch):
    def fake_get(url, timeout=None, headers=None):
        r = MagicMock(); r.status_code = 200; r.content = url.encode() + b"x" * 6000
        return r
    monkeypatch.setattr(img.requests, "get", fake_get)


def _sources(monkeypatch, fits_everyday_only=True):
    def fn(query):
        return [{"id": query, "url": f"http://a/{query}", "alt": f"photo {query}", "attribution": ""}]
    monkeypatch.setattr(img, "SOURCES", [("a", fn)])
    monkeypatch.setattr(pic.llm, "llm_pick_image", lambda w, s, alts: [0])
    monkeypatch.setattr(pic.vision, "vision_fits",
                        lambda w, s, p, **k: (s == pic.everyday_sense(w)) if fits_everyday_only else True)


# ── core：find_picture 回報圖用的是哪個詞義 ──────────────────────────────────

def test_fallback_reports_everyday_sense(tmp_path, monkeypatch, fast_download):
    _sources(monkeypatch)
    status = {}
    ok, _, _, source = pic.find_picture("concrete", str(tmp_path / "o.jpg"), "class diagram", "具體類別",
                                        rejects=[], status=status)
    assert ok and source == "a:concrete"
    assert status == {"sense": pic.everyday_sense("concrete")}


def test_first_stage_hit_reports_no_sense(tmp_path, monkeypatch, fast_download):
    _sources(monkeypatch, fits_everyday_only=False)
    status = {}
    assert pic.find_picture("oven", str(tmp_path / "o.jpg"), "oven", "烤箱", rejects=[], status=status)[0]
    assert status == {}


def test_no_everyday_skips_second_stage(tmp_path, monkeypatch, fast_download):
    _sources(monkeypatch)
    status = {}
    out = tmp_path / "o.jpg"
    assert pic.find_picture("concrete", str(out), "class diagram", "具體類別", rejects=[], status=status,
                            everyday=False) == (False, "", "", "")
    assert status == {} and not out.exists()


# ── helper：SENSE 行與 --no-everyday ────────────────────────────────────────

def _run_helper(monkeypatch, capsys, argv, find):
    import _image_helper as h
    class Done(Exception):
        pass
    monkeypatch.setattr(h, "_exit", lambda code: (_ for _ in ()).throw(Done(code)))
    monkeypatch.setattr(h, "find_picture", find)
    monkeypatch.setattr("sys.argv", ["_image_helper.py"] + argv)
    with pytest.raises(Done) as e:
        h.main()
    return e.value.args[0], capsys.readouterr().out


def test_helper_prints_fallback_sense(monkeypatch, capsys):
    def find(w, p, q, s, status=None, everyday=True):
        status["sense"] = "everyday concrete"
        return True, "", "A concrete wall.", "a:1"
    code, out = _run_helper(monkeypatch, capsys, ["--query", "q", "--sense", "s", "--", "w", "/tmp/x.jpg"], find)
    assert code == 0 and "SENSE: everyday concrete" in out


def test_helper_no_everyday_flag(monkeypatch, capsys):
    seen = {}
    def find(w, p, q, s, status=None, everyday=True):
        seen["everyday"] = everyday
        return True, "", "x", "a:1"
    code, out = _run_helper(monkeypatch, capsys,
                            ["--query", "q", "--sense", "s", "--no-everyday", "--", "w", "/tmp/x.jpg"], find)
    assert code == 0 and seen == {"everyday": False} and "SENSE" not in out


# ── addon：_fetch_image 解析 SENSE、帶 --no-everyday ─────────────────────────

def test_addon_fetch_image_reads_sense_and_flag(tmp_path, monkeypatch):
    cmds = []
    def run(self, cmd, timeout):
        cmds.append(cmd)
        return 0, "ALT: A concrete wall.\nSOURCE: a:1\nSENSE: everyday concrete\n", ""
    monkeypatch.setattr(addon._workers.Worker, "_run_helper", run)
    monkeypatch.setattr(addon._llm, "_llm_image_query", lambda w, d="": "q")
    w = addon._workers.Worker.__new__(addon._workers.Worker); w.media_dir = str(tmp_path)
    meta = {}
    assert "<img" in w._fetch_image("concrete", definition="s", meta=meta)
    assert meta == {"sense": "everyday concrete"} and "--no-everyday" not in cmds[-1]
    w._fetch_image("concrete", definition="s", everyday=False)
    assert "--no-everyday" in cmds[-1]


# ── ⌘A：圖退回日常義 → 例句、翻譯用日常義 ───────────────────────────────────

class _Emit:
    def __init__(self): self.calls = []
    def emit(self, *a): self.calls.append(a)


def test_cmd_a_uses_picture_sense():
    w = addon._workers.Worker.__new__(addon._workers.Worker)
    w.word, w.association, w.media_dir = "concrete", "具體類別", "/tmp"
    w.step, w.finished, w.error = _Emit(), _Emit(), _Emit()
    seen = {}
    def fetch(word, definition="", meta=None, **kw):
        meta["sense"] = "everyday concrete"
        return '<img src="a.jpg" alt="A concrete wall.">'
    w._fetch_image = fetch
    w._llm_sentence = lambda word, association="", photo="": seen.update(sentence=association) or ("The wall is concrete.", "Groq")
    w._groq_translate = lambda word, s, sense="", **kw: seen.update(translation=sense) or "混凝土"
    w._groq_translate_sentence = lambda s, word="", sense="", **kw: seen.update(sentence_cn=sense) or "牆是混凝土。"
    w._make_audio_batch = lambda items: None
    w.run()
    assert seen == {"sentence": "everyday concrete", "translation": "everyday concrete",
                    "sentence_cn": "everyday concrete"}


# ── ⌘S：只缺圖 → 不准退回日常義；整張重做 → 用圖的詞義 ─────────────────────

class _Resp:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return b'{"error": null}'


def _run_s(sentence, fetch_sense=""):
    vals = {"Front": "concrete", "Sentence": sentence, "Image_Prompt": "", "Audio": "",
            "Front_Audio": "[sound:x.mp3]", "Translation": "", "Sentence_CN": "", "Association": "具體類別"}
    note = {"noteId": 1, "fields": {k: {"value": v} for k, v in vals.items()}}
    bw = addon._workers.BackfillWorker.__new__(addon._workers.BackfillWorker)
    bw.media_dir = "/tmp"
    bw._hit_limit = False; bw._stopped = False; bw.retry_after = 0; bw.limit_resets = {}
    bw.step, bw.card_done = _Emit(), _Emit()
    seen = {}
    def fetch(word, definition="", meta=None, everyday=True, **kw):
        seen["everyday"] = everyday
        if fetch_sense and everyday:
            meta["sense"] = fetch_sense
        return '<img src="a.jpg" alt="A concrete wall.">'
    w = SimpleNamespace(
        _pick_sense=lambda word: "",
        _fetch_image=fetch,
        _llm_sentence=lambda word, association="", photo="": seen.update(sentence=association) or ("The wall is concrete.", "Groq"),
        _groq_translate=lambda word, s, sense="", **kw: seen.update(translation=sense) or "混凝土",
        _groq_translate_sentence=lambda s, strict=False, word="", sense="", **kw: seen.update(sentence_cn=sense) or "牆。",
        _make_audio_batch=lambda items: None,
    )
    bw._w = w
    def fake_urlopen(req, timeout=None):
        seen["sent"] = json.loads(req.data.decode())["params"]["note"]["fields"]
        return _Resp()
    with patch.object(addon._llm._dispatcher, "wall_secs", return_value=0), \
         patch.object(addon._workers.urllib.request, "urlopen", fake_urlopen):
        bw._process_one(note)
    return seen


def test_cmd_s_whole_card_uses_picture_sense():
    seen = _run_s("", fetch_sense="everyday concrete")
    assert seen["everyday"] is True
    assert seen["sentence"] == seen["translation"] == seen["sentence_cn"] == "everyday concrete"


def test_cmd_s_only_image_missing_no_fallback():
    seen = _run_s("The developer wrote a concrete class.", fetch_sense="everyday concrete")
    assert seen["everyday"] is False and "sentence" not in seen
    assert seen["translation"] == "具體類別"


# ── CLI ─────────────────────────────────────────────────────────────────────

def test_cli_backfill_uses_picture_sense(monkeypatch):
    import backfill_words as b
    seen = {}
    def find(w, p, q, s, status=None, everyday=True):
        seen["everyday"] = everyday
        status["sense"] = "everyday concrete"
        return True, "", "A concrete wall.", "a:1"
    monkeypatch.setattr(b, "find_picture", find)
    monkeypatch.setattr(b, "llm_image_query", lambda w, s: "q")
    monkeypatch.setattr(b, "llm_sentence", lambda w, association="", photo="": seen.update(sentence=association) or "The wall is concrete.")
    monkeypatch.setattr(b, "llm_translate", lambda w, s, sense="": seen.update(translation=sense) or "混凝土")
    monkeypatch.setattr(b, "try_make_audio", lambda *a, **k: True)
    monkeypatch.setattr(b, "anki", lambda *a, **k: None)
    vals = {"Front": "concrete", "Sentence": "", "Image_Prompt": "", "Audio": "", "Front_Audio": "",
            "Translation": "", "Association": "具體類別"}
    b.process_note({"noteId": 1, "fields": {k: {"value": v} for k, v in vals.items()}})
    assert seen == {"everyday": True, "sentence": "everyday concrete", "translation": "everyday concrete"}
