"""挑圖：LLM 從描述挑 → 下載 → 看圖確認（2026-10-10）。
事故：搜圖拿第一張下載得到的就用——concrete 配到生物課老師、instance 配到 1922 年電話雜誌；
只看描述挑也不準（concrete 挑到 art class、harness 挑到馬具），所以下載後再讓看圖模型確認。
不打網路：搜尋、下載、LLM、看圖全部 patch。"""
import json
import time
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
        assert "1. A teacher" in p and "2. (no description)" in p and "具體類別" in p
        assert "up to 3" in p and "NONE" in p

    @pytest.mark.parametrize("reply,expected", [
        ("2", [1]), ("3, 1", [2, 0]), (" 3. Code on a laptop", [2]), ("2, 2, 1", [1, 0]),
        ("NONE", []), ("none of them", []), ("", None), ("9", None), ("0", None), ("maybe the second", None),
    ])
    def test_parse(self, reply, expected):
        assert llm_mod.parse_image_pick(reply, 3) == expected

    def test_pick_asks_with_sense(self, monkeypatch):
        seen = []
        monkeypatch.setattr(llm_mod, "llm", lambda p, **kw: seen.append(p) or "2")
        assert REAL_CORE_PICK("concrete", "具體類別", ["teacher", "code"]) == [1]
        assert "具體類別" in seen[0]


# ── fetch_judged：排前 3 → 同時下載、同時看圖 → 取第一張對的 ───────────────────

def _url_of(path):
    return open(path, "rb").read()[:20].decode(errors="ignore")


class TestFetchJudged:
    def test_takes_first_true_in_rank_order(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "art class", "wall", "code", "laptop"))])
        looked = []
        def verify(path):
            looked.append(_url_of(path))
            return "a/2" in looked[-1] or "a/3" in looked[-1]
        out = tmp_path / "o.jpg"
        found = img.fetch_judged(str(out), "q", [], lambda alts: [0, 3, 2], verify)
        assert found[2:] == ("laptop", "a:3")                 # 排名第二的 3 先於第三的 2
        assert len(looked) == 3 and "a/3" in _url_of(out)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["o.jpg"]   # 暫存檔清乾淨

    def test_all_wrong_moves_to_next_source(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "x", "y")), _src("b", _cands("b", "code"))])
        found = img.fetch_judged(str(tmp_path / "o.jpg"), "q", [], lambda alts: [0, 1],
                                 lambda path: "http://b" in _url_of(path))
        assert found[3] == "b:0"

    def test_none_skips_source_without_download(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "teacher")), _src("b", _cands("b", "code"))])
        looked = []
        found = img.fetch_judged(str(tmp_path / "o.jpg"), "q", [],
                                 lambda alts: [] if alts == ["teacher"] else [0],
                                 lambda path: looked.append(path) or True)
        assert found[3] == "b:0" and len(looked) == 1

    def test_unverifiable_is_not_accepted(self, tmp_path, monkeypatch, fast_download):
        """看不了（None）的圖不收——2026-10-18 起只有看圖說 YES 的才收。"""
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "x", "y"))])
        answers = {"a/0": False, "a/1": None}
        out = tmp_path / "o.jpg"
        found = img.fetch_judged(str(out), "q", [], lambda alts: [0, 1],
                                 lambda path: answers[next(k for k in answers if k in _url_of(path))])
        assert found is None and not out.exists()

    def test_judge_failure_checks_first_three(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "w", "x", "y", "z"))])
        looked = []
        found = img.fetch_judged(str(tmp_path / "o.jpg"), "q", [], lambda alts: None,
                                 lambda path: looked.append(path) or "a/2" in _url_of(path))
        assert found[3] == "a:2" and len(looked) == 3

    def test_rejected_candidates_never_shown(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "old pick", "new"))])
        seen = []
        img.fetch_judged(str(tmp_path / "o.jpg"), "q", ["a:0"],
                         lambda alts: seen.append(alts) or [0], lambda path: True)
        assert seen == [["new"]]

    def test_nothing_fits_returns_none_and_cleans_up(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", _cands("a", "x", "y"))])
        assert img.fetch_judged(str(tmp_path / "o.jpg"), "q", [], lambda alts: [0, 1], lambda p: False) is None
        assert list(tmp_path.iterdir()) == []


# ── find_picture：詞義 → 日常義 → 沒圖（沒有退路）───────────────────────────────

class TestFindPicture:
    def _setup(self, monkeypatch, fits):
        """fits(sense, query) → 看圖結果。記下每次看圖用的詞義與搜尋字串。"""
        calls = []
        def fn(query):
            return [{"id": "1", "url": f"http://a/{query}", "alt": f"photo for {query}", "attribution": ""}]
        monkeypatch.setattr(img, "SOURCES", [("a", fn)])
        monkeypatch.setattr(pic.llm, "llm_pick_image", lambda w, s, alts: [0])
        def vf(word, sense, path, **k):
            q = open(path, "rb").read().decode(errors="ignore").split("http://a/")[1].split("x")[0]
            calls.append((sense, q))
            return fits(sense, q)
        monkeypatch.setattr(pic.vision, "vision_fits", vf)
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

    def test_tech_word_nothing_fits_is_no_image(self, tmp_path, monkeypatch, fast_download):
        """技術字也一樣：沒有看圖說對的就沒圖，不再塞通用程式畫面（2026-10-18 使用者否決）。"""
        calls = self._setup(monkeypatch, lambda s, q: False)
        out = tmp_path / "o.jpg"
        assert pic.find_picture("instance", str(out), "object", "物件實例", rejects=[]) == (False, "", "", "")
        assert [q for _, q in calls] == ["object", "instance"] and not out.exists()

    def test_everyday_nothing_fits_no_image_and_no_leftover(self, tmp_path, monkeypatch, fast_download):
        self._setup(monkeypatch, lambda s, q: False)
        out = tmp_path / "o.jpg"
        assert pic.find_picture("whim", str(out), "q", "一時興起", rejects=[]) == (False, "", "", "")
        assert not out.exists()

    def test_vision_budget_exhausted_is_no_image(self, tmp_path, monkeypatch, fast_download):
        """看圖額度用完 → 後面的圖看不了 → 沒圖，不照收。額度每一輪各算：第一輪用完，
        第二輪（日常義）照樣看得到（2026-10-26 inherit：第一輪看滿 6 張，懷錶那輪一張都沒看就停卡）。"""
        monkeypatch.setattr(pic, "MAX_VISION_CHECKS", 1)
        calls = self._setup(monkeypatch, lambda s, q: False)
        assert pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[]) == (False, "", "", "")
        assert [s for s, _ in calls] == ["s", pic.everyday_sense("w")]

    def test_second_stage_sees_photos_after_first_used_budget(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(pic, "MAX_VISION_CHECKS", 1)
        self._setup(monkeypatch, lambda s, q: q == "w")
        assert pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[])[0]

    def test_unverifiable_everywhere_is_no_image(self, tmp_path, monkeypatch, fast_download):
        """看圖全回 None（沒金鑰、撞限）→ 沒圖；呼叫端會停卡。"""
        self._setup(monkeypatch, lambda s, q: None)
        assert pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[]) == (False, "", "", "")


# ── vision：解析與換模型 ──────────────────────────────────────────────────────

class TestVision:
    @pytest.fixture(autouse=True)
    def _cool_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vision, "COOLDOWN_PATH", str(tmp_path / "cool.json"))
        monkeypatch.setattr(vision, "_load_key", lambda: "k")

    def _img(self, tmp_path):
        p = tmp_path / "i.jpg"; p.write_bytes(b"\xff\xd8" + b"x" * 100)
        return str(p)

    def test_yes_no(self, tmp_path, monkeypatch):
        for reply, expected in [("YES", True), ("No.", False)]:
            monkeypatch.setattr(vision, "_ask", lambda m, k, p, d, r=reply: (r, 200, ""))
            assert REAL_VISION("w", "s", self._img(tmp_path)) is expected

    def test_rate_limited_model_is_skipped_across_processes(self, tmp_path, monkeypatch):
        asked = []
        def ask(model, k, p, d):
            asked.append(model)
            return (None, 429, "") if model == vision.VISION_MODELS[0] else ("YES", 200, "")
        monkeypatch.setattr(vision, "_ask", ask)
        assert REAL_VISION("w", "s", self._img(tmp_path)) is True
        assert REAL_VISION("w", "s", self._img(tmp_path)) is True
        assert asked == vision.VISION_MODELS[:2] + [vision.VISION_MODELS[1]]   # 冷卻記在檔案，第二次直接跳過
        assert vision.VISION_MODELS[0] in json.load(open(vision.COOLDOWN_PATH))

    def test_daily_quota_cools_until_reset(self, tmp_path, monkeypatch):
        body = '{"error":{"details":[{"violations":[{"quotaId":"GenerateRequestsPerDayPerProjectPerModel"}]},{"retryDelay":"68000s"}]}}'
        monkeypatch.setattr(vision, "_ask", lambda *a: (None, 429, body))
        REAL_VISION("w", "s", self._img(tmp_path))
        until = json.load(open(vision.COOLDOWN_PATH))[vision.VISION_MODELS[0]]
        assert until - time.time() > 60000

    def test_no_key_or_all_down_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(vision, "_load_key", lambda: "")
        assert REAL_VISION("w", "s", self._img(tmp_path)) is None
        monkeypatch.setattr(vision, "_load_key", lambda: "k")
        monkeypatch.setattr(vision, "RETRY_PAUSE_SECS", 0)
        monkeypatch.setattr(vision, "_ask", lambda *a: (None, 503, ""))
        assert REAL_VISION("w", "s", self._img(tmp_path)) is None

    def test_timeout_and_server_error_do_not_cool(self, tmp_path, monkeypatch):
        """逾時、5xx 是單次的慢或忙，不冷卻：2026-10-26 一次逾時就冷卻 60 秒，同一張卡後面的圖全部看不了、停卡。"""
        import requests
        def ask(model, k, p, d):
            if model == vision.VISION_MODELS[0]:
                raise requests.ReadTimeout("slow")
            return None, 503, ""
        monkeypatch.setattr(vision, "RETRY_PAUSE_SECS", 0)
        monkeypatch.setattr(vision, "_ask", ask)
        assert REAL_VISION("w", "s", self._img(tmp_path)) is None
        assert vision._load_cooldown() == {}

    def test_server_error_retried_once_timeout_not(self, tmp_path, monkeypatch):
        """5xx 回得快 → 第二輪再問一次；逾時的模型第二輪不再等。"""
        import requests
        asked = []
        busy = {"n": 0}
        def ask(model, k, p, d):
            asked.append(model)
            if model == vision.VISION_MODELS[0]:
                raise requests.ReadTimeout("slow")
            busy["n"] += 1
            return (None, 503, "") if busy["n"] == 1 else ("YES", 200, "")
        monkeypatch.setattr(vision, "RETRY_PAUSE_SECS", 0)
        monkeypatch.setattr(vision, "_ask", ask)
        assert REAL_VISION("w", "s", self._img(tmp_path)) is True
        assert asked == [vision.VISION_MODELS[0], vision.VISION_MODELS[1], vision.VISION_MODELS[1]]


class TestPictureStatus:
    """找不到圖時分清楚：看圖服務不能用（下次再試）vs 看過了都不對。"""

    def _one_source(self, monkeypatch, verdict, pick=(0,)):
        def fn(query):
            return [{"id": "1", "url": f"http://a/{query}", "alt": "photo", "attribution": ""}]
        monkeypatch.setattr(img, "SOURCES", [("a", fn)])
        monkeypatch.setattr(pic.llm, "llm_pick_image", lambda w, s, alts: list(pick))
        monkeypatch.setattr(pic.vision, "vision_fits", lambda w, s, p, **k: verdict)

    def test_all_unverifiable_marks_unavailable(self, tmp_path, monkeypatch, fast_download):
        self._one_source(monkeypatch, None)
        status = {}
        assert pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[], status=status)[0] is False
        assert status == {"vision": "unavailable"}

    def test_seen_and_rejected_is_not_unavailable(self, tmp_path, monkeypatch, fast_download):
        self._one_source(monkeypatch, False)
        status = {}
        pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[], status=status)
        assert status == {}

    def test_no_candidates_is_not_unavailable(self, tmp_path, monkeypatch, fast_download):
        self._one_source(monkeypatch, None, pick=())
        status = {}
        pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[], status=status)
        assert status == {}

    def test_status_optional(self, tmp_path, monkeypatch, fast_download):
        self._one_source(monkeypatch, None)
        assert pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[]) == (False, "", "", "")


class TestPerStepTimeouts:
    """整段沒有時間上限（2026-10-18）；單次挑圖／看圖各自有逾時，逾時＝這張看不了＝不收。"""

    def _sources(self, monkeypatch):
        def fn(query):
            return [{"id": "1", "url": f"http://a/{query}", "alt": f"photo for {query}", "attribution": ""}]
        monkeypatch.setattr(img, "SOURCES", [("a", fn), ("b", fn)])

    def test_slow_judge_is_cut_then_first_three_checked(self, tmp_path, monkeypatch, fast_download):
        self._sources(monkeypatch)
        monkeypatch.setattr(pic, "JUDGE_TIMEOUT_SECS", 0.2)
        monkeypatch.setattr(pic.llm, "llm_pick_image", lambda w, s, alts: time.sleep(5) or [0])
        monkeypatch.setattr(pic.vision, "vision_fits", lambda w, s, p, **k: True)
        t = time.monotonic()
        ok, _, desc, _ = pic.find_picture("w", str(tmp_path / "o.jpg"), "q", "s", rejects=[])
        assert ok and desc == "photo for q"            # 挑圖逾時 → 拿前三張去看 → 看圖說對就收
        assert time.monotonic() - t < 3

    def test_slow_vision_is_not_accepted(self, tmp_path, monkeypatch, fast_download):
        self._sources(monkeypatch)
        monkeypatch.setattr(pic, "VISION_TIMEOUT_SECS", 0.2)
        monkeypatch.setattr(pic.llm, "llm_pick_image", lambda w, s, alts: [0])
        monkeypatch.setattr(pic.vision, "vision_fits", lambda w, s, p, **k: time.sleep(5) or True)
        out = tmp_path / "o.jpg"
        assert pic.find_picture("w", str(out), "q", "s", rejects=[]) == (False, "", "", "")
        assert not out.exists()                        # 看圖逾時＝看不了 → 不收，不是照收

    def test_timed_returns_none_on_timeout(self):
        assert pic._timed(lambda: time.sleep(1) or 1, 0.1) is None
        assert pic._timed(lambda: 7, 1) == 7
        assert pic._timed(lambda: 7, 0) is None


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
    monkeypatch.setattr(h, "find_picture", lambda w, p, q, s, **k: seen.update(q=q, s=s) or (True, "", "code", "a:1"))
    code, out = _run_helper(monkeypatch, capsys, ["--query", "q", "--sense", "具體類別", "--", "w", "/tmp/x.jpg"])
    assert code == 0 and seen == {"q": "q", "s": "具體類別"} and "SOURCE: a:1" in out


def test_helper_without_sense_picks_one_then_find_picture(monkeypatch, capsys):
    """沒帶詞義也要挑圖：helper 自己選一次詞義，再沒有用日常義。沒有「拿第一張」的路。"""
    import _image_helper as h
    seen = []
    monkeypatch.setattr(h, "find_picture", lambda w, p, q, s, **k: seen.append(s) or (True, "", "x", "a:2"))
    monkeypatch.setattr(h, "llm_pick_sense", lambda w: "具體類別")
    code, out = _run_helper(monkeypatch, capsys, ["--query", "q", "--", "w", "/tmp/x.jpg"])
    assert code == 0 and "SOURCE: a:2" in out and seen == ["具體類別"]
    monkeypatch.setattr(h, "llm_pick_sense", lambda w: "")
    _run_helper(monkeypatch, capsys, ["--query", "q", "--", "w", "/tmp/x.jpg"])
    assert seen[-1] == pic.everyday_sense("w")


def test_helper_no_picture_exits_1(monkeypatch, capsys):
    import _image_helper as h
    monkeypatch.setattr(h, "find_picture", lambda w, p, q, s, **k: (False, "", "", ""))
    code, out = _run_helper(monkeypatch, capsys, ["--query", "q", "--sense", "s", "--", "w", "/tmp/x.jpg"])
    assert code == 1 and "FAIL" in out and "SOURCE" not in out
    assert "photo check unavailable" not in out


def test_helper_vision_unavailable_says_so(monkeypatch, capsys):
    import _image_helper as h
    monkeypatch.setattr(h, "find_picture",
                        lambda w, p, q, s, status=None, **k: status.update(vision="unavailable") or (False, "", "", ""))
    code, out = _run_helper(monkeypatch, capsys, ["--query", "q", "--sense", "s", "--", "w", "/tmp/x.jpg"])
    assert code == 1 and "FAIL: photo check unavailable" in out


@pytest.mark.parametrize("stdout, short", [
    ("FAIL: photo check unavailable\n", "photo check unavailable, retry later"),
    ("FAIL: no usable image found\n", "no image"),
])
def test_addon_image_reason(tmp_path, monkeypatch, stdout, short):
    """畫面要分得出「看圖服務暫時不能用」和「真的沒圖」（2026-10-26 oven 停卡卻只寫 no image）。"""
    monkeypatch.setattr(addon._workers.Worker, "_run_helper", lambda self, cmd, t: (1, stdout, "err"))
    monkeypatch.setattr(addon._llm, "_llm_image_query", lambda w, d="": "q")
    w = addon._workers.Worker.__new__(addon._workers.Worker); w.media_dir = str(tmp_path)
    reasons = {}
    assert w._fetch_image("oven", definition="s", reasons=reasons) == ""
    assert reasons == {"image": short}


def test_addon_passes_sense_and_no_timeout(tmp_path, monkeypatch):
    """找圖沒有時間上限（2026-10-18）：helper 不被 addon 殺掉，自己每一步有逾時。"""
    seen = {}
    def run(self, cmd, timeout):
        seen.update(cmd=cmd, timeout=timeout)
        return 1, "", ""
    monkeypatch.setattr(addon._workers.Worker, "_run_helper", run)
    monkeypatch.setattr(addon._llm, "_llm_image_query", lambda w, d="": "q")
    w = addon._workers.Worker.__new__(addon._workers.Worker); w.media_dir = str(tmp_path)
    w._fetch_image("concrete", definition="具體類別")
    assert seen["cmd"][seen["cmd"].index("--sense") + 1] == "具體類別"
    assert seen["timeout"] is None
    w._fetch_image("concrete")
    assert "--sense" not in seen["cmd"] and seen["timeout"] is None


def test_cli_backfill_always_find_picture(monkeypatch):
    """CLI 也一律挑圖：有詞義用詞義；沒詞義先選一次，再沒有用日常義。"""
    import backfill_words
    senses = []
    monkeypatch.setattr(backfill_words, "find_picture", lambda w, p, q, s, **k: senses.append(s) or (False, "", "", ""))
    monkeypatch.setattr(backfill_words, "llm_pick_sense", lambda w: "")
    backfill_words._do_image("concrete", "q", sense="具體類別")
    backfill_words._do_image("concrete", "q")
    assert senses == ["具體類別", pic.everyday_sense("concrete")]


def test_image_sense_prefers_hint_then_sentence():
    """只缺圖時圖要配卡上那句（圖文要對上）；addon 與 CLI 同一條規則。"""
    import backfill_words
    for fn in (addon._workers._image_sense, backfill_words._image_sense):
        assert fn("具體類別", "The Logger class is concrete.") == "具體類別"
        assert fn("", "The Logger class is concrete.") == 'as used in this sentence: "The Logger class is concrete."'
        assert fn("", "") == "" and fn("", "Please add an example sentence for 'x'.") == ""


# ── 畫面進度：helper 的 PROGRESS 行、換模型、⌘S 三張同時跑不互蓋 ─────────────────

def test_run_helper_streams_progress_lines(tmp_path):
    import sys as _sys
    script = tmp_path / "fake_helper.py"
    script.write_text(
        "import sys, time\n"
        "print('PROGRESS_BUDGET: 10', file=sys.stderr, flush=True)\n"
        "print('PROGRESS: searching Pexels', file=sys.stderr, flush=True)\n"
        "print('[picture] debug line', file=sys.stderr, flush=True)\n"
        "print('ALT: a glacier')\n")
    said = []
    prog = addon._workers._Progress(lambda text, countdown: said.append((text, countdown)))
    prog.phase = "Image"
    w = addon._workers.Worker.__new__(addon._workers.Worker)
    addon._workers._bind_progress(prog)
    try:
        rc, out, err = w._run_helper([_sys.executable, str(script)], 10)
    finally:
        addon._workers._bind_progress(None)
    assert rc == 0 and "ALT: a glacier" in out and "[picture] debug line" in err
    assert said == [("Image · finding a photo", 10), ("Image · searching Pexels", 0)]


def test_model_switch_goes_to_the_card_on_this_thread():
    import threading as _t
    said = {"a": [], "b": []}
    progs = {k: addon._workers._Progress(lambda text, c, k=k: said[k].append(text)) for k in said}
    def card(k, model):
        addon._workers._bind_progress(progs[k])
        try:
            addon._workers._set_phase("Sentence")
            addon._llm._dispatcher.listener("try", model)
            addon._llm._dispatcher.listener("fail", model)
        finally:
            addon._workers._bind_progress(None)
    ts = [_t.Thread(target=card, args=("a", "groq:openai/gpt-oss-120b")),
          _t.Thread(target=card, args=("b", "gemini:gemini-3.7-flash"))]
    for t in ts: t.start()
    for t in ts: t.join()
    assert said["a"] == ["Sentence · starting", "Sentence · gpt-oss-120b (groq)",
                         "Sentence · gpt-oss-120b (groq) no reply, trying the next model"]
    assert said["b"][1] == "Sentence · gemini-3.7-flash (gemini)"


def test_no_progress_registered_is_silent():
    addon._llm._dispatcher.listener("try", "groq:x")      # 測試、⌘F 翻譯：沒登記 → 不炸
    addon._workers._set_phase("Image")


# ── 看圖回覆解析 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply, expected", [
    ("YES", True),
    ("**YES**", True),
    ("No.", False),
    ("maybe", None),
    ("", None),
])
def test_parse_answer(reply, expected):
    assert vision.parse_answer(reply) is expected


def test_unreadable_file_is_not_a_fit(tmp_path, monkeypatch):
    """圖源回了 HTML 之類：Gemini 回 400 → 當這張不對，不當「看不了」（不然畫面一直說稍後再試）。
    金鑰無效的 400 仍算看不了。"""
    monkeypatch.setattr(vision, "COOLDOWN_PATH", str(tmp_path / "cool.json"))
    monkeypatch.setattr(vision, "_load_key", lambda: "k")
    p = tmp_path / "i.jpg"; p.write_bytes(b"<html>" + b"x" * 100)
    monkeypatch.setattr(vision, "_ask", lambda *a: (None, 400, '{"error":{"message":"Unable to process input image"}}'))
    assert REAL_VISION("w", "s", str(p)) is False
    monkeypatch.setattr(vision, "_ask", lambda *a: (None, 400, '{"error":{"status":"INVALID_ARGUMENT","details":[{"reason":"API_KEY_INVALID"}]}}'))
    assert REAL_VISION("w", "s", str(p)) is None
