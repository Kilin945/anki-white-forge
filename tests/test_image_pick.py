"""挑圖：LLM 從描述挑 → 下載 → 看圖確認（2026-10-10）。
事故：搜圖拿第一張下載得到的就用——concrete 配到生物課老師、instance 配到 1922 年電話雜誌；
只看描述挑也不準（concrete 挑到 art class、harness 挑到馬具），所以下載後再讓看圖模型確認。
不打網路：搜尋、下載、LLM、看圖全部 patch。"""
from unittest.mock import MagicMock

import pytest

import addon
import core.image as img
import core.llm as llm_mod
import core.picture as pic
import core.vision as vision
from test_image_sources import _src

REAL_CORE_PICK = llm_mod.llm_pick_image
REAL_VISION = vision.vision_fits


@pytest.fixture
def fast_download(monkeypatch):
    def fake_get(url, timeout=None, headers=None):
        r = MagicMock(); r.status_code = 200; r.content = url.encode() + b"x" * 6000
        return r
    monkeypatch.setattr(img.requests, "get", fake_get)


def _cands(prefix, *alts):
    return [{"id": str(i), "url": f"http://{prefix}/{i}.jpg", "alt": a} for i, a in enumerate(alts)]


# ── 從描述挑：prompt 與解析 ───────────────────────────────────────────────────

class TestPickPrompt:
    def test_prompt_lists_numbered_descriptions(self):
        p = llm_mod.image_pick_prompt("concrete", "具體類別", ["A teacher", "", "Code on a laptop"])
        assert "1. A teacher" in p and "2. (no description)" in p and "具體類別" in p and "NONE" in p

    @pytest.mark.parametrize("reply,expected", [
        ("2", 1), (" 3. Code on a laptop", 2), ("NONE", img.PICK_NONE), ("none of them", img.PICK_NONE),
        ("", None), ("9", None), ("0", None), ("maybe the second", None),
    ])
    def test_parse(self, reply, expected):
        assert llm_mod.parse_image_pick(reply, 3) == expected

    def test_pick_asks_with_sense(self, monkeypatch):
        seen = []
        monkeypatch.setattr(llm_mod, "llm", lambda p, **kw: seen.append(p) or "2")
        assert REAL_CORE_PICK("concrete", "具體類別", ["teacher", "code"]) == 1
        assert "具體類別" in seen[0]


# ── fetch_judged：挑 → 下載 → 看圖 ───────────────────────────────────────────

class TestFetchJudged:
    def test_vision_no_moves_to_next_source(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "art class")),
                                             _src("b", _cands("b", "code"))])
        looked = []
        def verify(path):
            looked.append(open(path, "rb").read()[:12])
            return b"http://b" in looked[-1]
        found = img.fetch_judged(str(tmp_path / "o.jpg"), "q", [], lambda alts: 0, verify)
        assert found[2:] == ("code", "b:0")
        assert len(looked) == 2

    def test_judge_none_skips_source_without_download(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "teacher")),
                                             _src("b", _cands("b", "code"))])
        looked = []
        found = img.fetch_judged(str(tmp_path / "o.jpg"), "q", [],
                                 lambda alts: img.PICK_NONE if alts == ["teacher"] else 0,
                                 lambda path: looked.append(path) or True)
        assert found[3] == "b:0" and len(looked) == 1

    def test_vision_unavailable_accepts_pick(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "teacher", "code"))])
        found = img.fetch_judged(str(tmp_path / "o.jpg"), "q", [], lambda alts: 1, lambda path: None)
        assert found[3] == "a:1"

    def test_judge_failure_tries_candidates_in_order(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "x", "y", "z"))])
        answers = iter([False, True])
        found = img.fetch_judged(str(tmp_path / "o.jpg"), "q", [], lambda alts: None,
                                 lambda path: next(answers))
        assert found[3] == "a:1"

    def test_rejected_candidates_never_shown(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "old pick", "new"))])
        seen = []
        img.fetch_judged(str(tmp_path / "o.jpg"), "q", ["a:0"],
                         lambda alts: seen.append(alts) or 0, lambda path: True)
        assert seen == [["new"]]

    def test_nothing_fits_returns_none(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "x"))])
        assert img.fetch_judged(str(tmp_path / "o.jpg"), "q", [], lambda alts: 0, lambda p: False) is None


# ── find_picture：詞義 → 日常義 → 通用程式畫面 ───────────────────────────────

class TestFindPicture:
    def _setup(self, monkeypatch, fits, tech=False):
        """fits(sense, query) → 看圖結果。記下每次看圖用的詞義與搜尋字串。"""
        calls = []
        def fn(query):
            return [{"id": "1", "url": f"http://a/{query}", "alt": f"photo for {query}", "attribution": ""}]
        monkeypatch.setattr(img, "SOURCES", [("a", fn)])
        monkeypatch.setattr(pic.llm, "llm_pick_image", lambda w, s, alts: 0)
        def vf(word, sense, path):
            q = open(path, "rb").read().decode(errors="ignore").split("http://a/")[1].split("x")[0]
            calls.append((sense, q))
            return fits(sense, q)
        monkeypatch.setattr(pic.vision, "vision_fits", vf)
        monkeypatch.setattr(pic.llm, "llm_is_tech", lambda w, s: tech)
        return calls

    def test_sense_fits_first(self, tmp_path, monkeypatch, fast_download):
        calls = self._setup(monkeypatch, lambda s, q: True)
        ok, _, desc, _ = pic.find_picture("glacier", str(tmp_path / "o.jpg"), "glacier ice", "冰河", rejects=[])
        assert ok and desc == "photo for glacier ice" and calls == [("冰河", "glacier ice")]

    def test_falls_back_to_everyday_meaning(self, tmp_path, monkeypatch, fast_download):
        calls = self._setup(monkeypatch, lambda s, q: q == "concrete")
        ok, _, desc, _ = pic.find_picture("concrete", str(tmp_path / "o.jpg"), "class diagram", "具體類別",
                                          rejects=[])
        assert ok and desc == "photo for concrete"
        assert calls[-1] == (pic.everyday_sense("concrete"), "concrete")

    def test_tech_last_resort_generic_code(self, tmp_path, monkeypatch, fast_download):
        self._setup(monkeypatch, lambda s, q: False, tech=True)
        ok, _, desc, _ = pic.find_picture("instance", str(tmp_path / "o.jpg"), "object", "物件實例", rejects=[])
        assert ok and desc == f"photo for {pic.GENERIC_TECH_QUERY}"

    def test_everyday_nothing_fits_no_image_and_no_leftover(self, tmp_path, monkeypatch, fast_download):
        self._setup(monkeypatch, lambda s, q: False, tech=False)
        out = tmp_path / "o.jpg"
        assert pic.find_picture("whim", str(out), "q", "一時興起", rejects=[]) == (False, "", "", "")
        assert not out.exists()

    def test_vision_budget_then_accepts(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(pic, "MAX_VISION_CHECKS", 1)
        calls = self._setup(monkeypatch, lambda s, q: False)
        ok, _, desc, _ = pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[])
        assert ok and desc == "photo for w" and len(calls) == 1     # 第二次沒額度看 → 照收


# ── vision：解析與換模型 ──────────────────────────────────────────────────────

class TestVision:
    def _img(self, tmp_path):
        p = tmp_path / "i.jpg"; p.write_bytes(b"\xff\xd8" + b"x" * 100)
        return str(p)

    def test_yes_no(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vision, "_load_key", lambda: "k")
        monkeypatch.setattr(vision, "_cool_until", {})
        for reply, expected in [("YES", True), ("No.", False)]:
            monkeypatch.setattr(vision, "_ask", lambda m, k, p, d, r=reply: (r, 200))
            assert REAL_VISION("w", "s", self._img(tmp_path)) is expected

    def test_rate_limited_model_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vision, "_load_key", lambda: "k")
        monkeypatch.setattr(vision, "_cool_until", {})
        asked = []
        def ask(model, k, p, d):
            asked.append(model)
            return (None, 429) if model == vision.VISION_MODELS[0] else ("YES", 200)
        monkeypatch.setattr(vision, "_ask", ask)
        assert REAL_VISION("w", "s", self._img(tmp_path)) is True
        assert REAL_VISION("w", "s", self._img(tmp_path)) is True
        assert asked == vision.VISION_MODELS[:2] + [vision.VISION_MODELS[1]]   # 第二次不再問冷卻中的

    def test_no_key_or_all_down_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vision, "_load_key", lambda: "")
        assert REAL_VISION("w", "s", self._img(tmp_path)) is None
        monkeypatch.setattr(vision, "_load_key", lambda: "k")
        monkeypatch.setattr(vision, "_cool_until", {})
        monkeypatch.setattr(vision, "_ask", lambda *a: (None, 503))
        assert REAL_VISION("w", "s", self._img(tmp_path)) is None


# ── 接線：helper、addon、CLI ─────────────────────────────────────────────────

def _run_helper(monkeypatch, capsys, argv):
    import _image_helper as h
    class Done(Exception):
        pass
    monkeypatch.setattr(h, "_exit", lambda code: (_ for _ in ()).throw(Done(code)))
    monkeypatch.setattr("sys.argv", ["_image_helper.py"] + argv)
    with pytest.raises(Done) as e:
        h.main()
    return e.value.args[0], capsys.readouterr().out


def test_helper_sense_uses_find_picture(monkeypatch, capsys):
    import _image_helper as h
    seen = {}
    monkeypatch.setattr(h, "find_picture", lambda w, p, q, s: seen.update(q=q, s=s) or (True, "", "code", "a:1"))
    monkeypatch.setattr(h, "fetch_image", lambda *a, **k: pytest.fail("不該走舊流程"))
    code, out = _run_helper(monkeypatch, capsys, ["--query", "q", "--sense", "具體類別", "--", "w", "/tmp/x.jpg"])
    assert code == 0 and seen == {"q": "q", "s": "具體類別"} and "SOURCE: a:1" in out


def test_helper_without_sense_keeps_old_flow(monkeypatch, capsys):
    import _image_helper as h
    monkeypatch.setattr(h, "find_picture", lambda *a, **k: pytest.fail("沒詞義不該挑圖"))
    monkeypatch.setattr(h, "fetch_image", lambda w, p, search_query=None: (True, "", "x", "a:2"))
    code, out = _run_helper(monkeypatch, capsys, ["--query", "q", "--", "w", "/tmp/x.jpg"])
    assert code == 0 and "SOURCE: a:2" in out


def test_addon_passes_sense_and_longer_timeout(tmp_path, monkeypatch):
    seen = {}
    def run(cmd, **kw):
        seen.update(cmd=cmd, timeout=kw["timeout"])
        return MagicMock(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(addon._workers.subprocess, "run", run)
    monkeypatch.setattr(addon._llm, "_llm_image_query", lambda w, d="": "q")
    w = addon._workers.Worker.__new__(addon._workers.Worker); w.media_dir = str(tmp_path)
    w._fetch_image("concrete", definition="具體類別")
    assert seen["cmd"][seen["cmd"].index("--sense") + 1] == "具體類別"
    assert seen["timeout"] == addon._workers.IMAGE_TIMEOUT_SECS
    w._fetch_image("concrete")
    assert "--sense" not in seen["cmd"] and seen["timeout"] == 60


def test_cli_backfill_uses_find_picture_only_with_sense(monkeypatch):
    import backfill_words
    used = []
    monkeypatch.setattr(backfill_words, "find_picture", lambda *a: used.append("pick") or (False, "", "", ""))
    monkeypatch.setattr(backfill_words, "fetch_image", lambda *a, **k: used.append("plain") or (False, "", "", ""))
    backfill_words._do_image("concrete", "q", sense="具體類別")
    backfill_words._do_image("concrete", "q")
    assert used == ["pick", "plain"]
