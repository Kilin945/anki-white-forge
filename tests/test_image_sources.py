# test_image_sources.py
"""多圖源搜圖：各家解析、排序、hedged 切換、退圖。無真實網路。"""
import json
import time
from unittest.mock import patch, MagicMock
import pytest
import core.image as img


def _resp(payload, status=200):
    r = MagicMock(status_code=status)
    r.json.return_value = payload
    r.raise_for_status = MagicMock() if status == 200 else MagicMock(side_effect=Exception("http"))
    return r


class TestSearchParsers:
    def test_pexels(self, monkeypatch):
        monkeypatch.setattr(img, "_load_pexels_key", lambda: "k")
        payload = {"photos": [{"id": 7, "src": {"large": "http://p/7.jpg"}, "alt": "A  cat.",
                               "photographer": "Ann", "url": "http://pexels/7"}]}
        with patch.object(img.requests, "get", return_value=_resp(payload)) as get:
            out = img._search_pexels("cat")
        assert out == [{"id": "7", "url": "http://p/7.jpg", "urls": ["http://p/7.jpg"], "alt": "A cat.",
                        "attribution": out[0]["attribution"]}]
        assert "Photo by Ann on" in out[0]["attribution"] and "http://pexels/7" in out[0]["attribution"]
        assert get.call_args.kwargs["headers"]["Authorization"] == "k"

    def test_pexels_without_key_returns_empty_without_request(self, monkeypatch):
        monkeypatch.setattr(img, "_load_pexels_key", lambda: "")
        with patch.object(img.requests, "get") as get:
            assert img._search_pexels("cat") == []
        get.assert_not_called()

    def test_wikimedia_title_is_html_escaped_in_attribution(self):
        payload = {"query": {"pages": {"1": {"pageid": 1, "index": 1, "title": "File:Clive & Mir <Jafar>\nline2.jpg",
                   "imageinfo": [{"thumburl": "http://w/t.jpg", "descriptionurl": "http://w/File:1"}]}}}}
        with patch.object(img.requests, "get", return_value=_resp(payload)):
            out = img._search_wikimedia("x")
        assert "&amp;" in out[0]["attribution"] and "&lt;" in out[0]["attribution"]
        assert "<Jafar>" not in out[0]["attribution"]
        assert "\n" not in out[0]["attribution"]            # 署名要單行（stdout 一行一筆）
        assert out[0]["alt"] == "Clive & Mir <Jafar> line2"

    def test_wikimedia_orders_by_index_and_uses_thumb(self):
        payload = {"query": {"pages": {
            "20": {"pageid": 20, "index": 2, "title": "File:Second painting.jpg",
                   "imageinfo": [{"thumburl": "http://w/2t.jpg", "url": "http://w/2.jpg", "descriptionurl": "http://w/File:2"}]},
            "10": {"pageid": 10, "index": 1, "title": "File:First painting.png",
                   "imageinfo": [{"thumburl": "http://w/1t.jpg", "url": "http://w/1.jpg", "descriptionurl": "http://w/File:1"}]},
        }}}
        with patch.object(img.requests, "get", return_value=_resp(payload)) as get:
            out = img._search_wikimedia("colonial painting")
        assert [c["id"] for c in out] == ["10", "20"]
        assert out[0]["url"] == "http://w/1t.jpg"                 # 縮圖不是原圖
        assert out[0]["alt"] == "First painting"                  # 去 File: 與副檔名
        assert "Wikimedia Commons" in out[0]["attribution"] and "http://w/File:1" in out[0]["attribution"]
        params = get.call_args.kwargs["params"]
        assert params["iiurlwidth"] == img.THUMB_WIDTH and "filetype:bitmap" in params["gsrsearch"]
        assert get.call_args.kwargs["headers"]["User-Agent"]

    def test_openverse_mature_true_and_fields(self):
        payload = {"results": [{"id": "ab-1", "thumbnail": "http://o/t.jpg", "url": "http://o/f.jpg",
                                "title": "Uncle Sam cartoon", "creator": "Puck", "foreign_landing_url": "http://flickr/x"}]}
        with patch.object(img.requests, "get", return_value=_resp(payload)) as get:
            out = img._search_openverse("imperialism cartoon")
        assert out[0] == {"id": "ab-1", "url": "http://o/t.jpg", "urls": ["http://o/t.jpg", "http://o/f.jpg"], "alt": "Uncle Sam cartoon", "attribution": out[0]["attribution"]}
        assert "By Puck via" in out[0]["attribution"] and "http://flickr/x" in out[0]["attribution"]
        assert get.call_args.kwargs["params"]["mature"] == "true"

    def test_openverse_emits_thumbnail_and_original_deduped(self):
        payload = {"results": [
            {"id": "a", "thumbnail": "http://o/t.jpg", "url": "http://o/f.jpg"},
            {"id": "b", "thumbnail": "http://o/same.jpg", "url": "http://o/same.jpg"},
            {"id": "c", "thumbnail": "", "url": "http://o/only.jpg"}]}
        with patch.object(img.requests, "get", return_value=_resp(payload)):
            out = img._search_openverse("x")
        assert [c["urls"] for c in out] == [["http://o/t.jpg", "http://o/f.jpg"],
                                            ["http://o/same.jpg"], ["http://o/only.jpg"]]

    def test_attribution_href_quoted_and_names_single_line(self):
        payload = {"results": [{"id": "a", "thumbnail": "http://o/t.jpg", "creator": "A\nB",
                                "foreign_landing_url": 'http://x/"onclick="y'}]}
        with patch.object(img.requests, "get", return_value=_resp(payload)):
            out = img._search_openverse("x")
        assert "\n" not in out[0]["attribution"] and 'href="http://x/&quot;' in out[0]["attribution"]

    def test_pixabay(self, monkeypatch):
        monkeypatch.setattr(img, "_load_pixabay_key", lambda: "pk")
        payload = {"hits": [{"id": 99, "webformatURL": "http://x/w.jpg", "largeImageURL": "http://x/l.jpg",
                             "tags": "ship, colonial", "user": "Bob", "pageURL": "http://pixabay/99"}]}
        with patch.object(img.requests, "get", return_value=_resp(payload)) as get:
            out = img._search_pixabay("ship")
        assert out[0]["id"] == "99" and out[0]["url"] == "http://x/w.jpg" and out[0]["alt"] == "ship, colonial"
        assert "Image by Bob on" in out[0]["attribution"]
        assert get.call_args.kwargs["params"]["key"] == "pk"

    def test_pixabay_without_key_returns_empty(self, monkeypatch):
        monkeypatch.setattr(img, "_load_pixabay_key", lambda: "")
        with patch.object(img.requests, "get") as get:
            assert img._search_pixabay("x") == []
        get.assert_not_called()

    def test_http_error_raises_and_safe_search_swallows(self):
        with patch.object(img.requests, "get", return_value=_resp({}, status=500)):
            with pytest.raises(Exception):
                img._search_wikimedia("x")
            assert img._safe_search(img._search_wikimedia, "x") == []


class TestRejectsAndOrder:
    def test_order_moves_rejected_sources_last_stable(self):
        names = ["pexels", "wikimedia", "openverse", "pixabay"]
        assert img.order_sources(names, []) == names
        assert img.order_sources(names, ["pexels:1"]) == ["wikimedia", "openverse", "pixabay", "pexels"]
        assert img.order_sources(names, ["pexels:", "openverse:9"]) == ["wikimedia", "pixabay", "pexels", "openverse"]
        assert img.order_sources(names, ["pexels:", "wikimedia:", "openverse:", "pixabay:"]) == names   # 全退過 → 原序

    def test_load_rejects_missing_or_broken_file(self, tmp_path):
        assert img.load_rejects("x", path=str(tmp_path / "none.json")) == []
        bad = tmp_path / "bad.json"; bad.write_text("{not json")
        assert img.load_rejects("x", path=str(bad)) == []
        empty = tmp_path / "empty.json"; empty.write_text("")
        assert img.load_rejects("x", path=str(empty)) == []

    def test_load_rejects_wrong_shapes(self, tmp_path):
        top = tmp_path / "top.json"; top.write_text("[]")
        assert img.load_rejects("w", path=str(top)) == []
        val = tmp_path / "val.json"; val.write_text(json.dumps({"w": "pexels:1"}))
        assert img.load_rejects("w", path=str(val)) == []

    def test_load_rejects_case_insensitive(self, tmp_path):
        p = tmp_path / "r.json"; p.write_text(json.dumps({"colonised": ["pexels:1"]}))
        assert img.load_rejects("Colonised", path=str(p)) == ["pexels:1"]
        assert img.load_rejects("other", path=str(p)) == []


def _src(name, candidates=None, delay=0.0, error=False, calls=None):
    """假圖源：回候選清單、可延遲、可拋錯；calls 記錄被叫到的圖源名。"""
    def fn(query):
        if calls is not None:
            calls.append(name)
        if delay:
            time.sleep(delay)
        if error:
            raise RuntimeError("boom")
        return [dict(c, attribution=f"<div>{name}</div>") for c in (candidates or [])]
    return (name, fn)


@pytest.fixture
def fast_download(monkeypatch):
    """下載一律成功（寫 6000 bytes），除非 url 含 'bad'。"""
    def fake_get(url, timeout=None, headers=None):
        r = MagicMock()
        if "bad" in url:
            r.status_code = 404; r.content = b""
        else:
            r.status_code = 200; r.content = b"x" * 6000
        return r
    monkeypatch.setattr(img.requests, "get", fake_get)


class TestFetchImageHedging:
    def test_primary_fast_hit_calls_only_one_source(self, tmp_path, monkeypatch, fast_download):
        calls = []
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", [{"id": "1", "url": "http://a/1.jpg", "alt": "A1"}], calls=calls),
            _src("b", [{"id": "9", "url": "http://b/9.jpg", "alt": "B9"}], calls=calls),
        ])
        ok, attr, desc, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[])
        assert (ok, desc, tag) == (True, "A1", "a:1") and attr == "<div>a</div>"
        assert calls == ["a"]
        assert (tmp_path / "o.jpg").stat().st_size == 6000

    def test_primary_empty_opens_next_immediately(self, tmp_path, monkeypatch, fast_download):
        calls = []
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", [], calls=calls),
            _src("b", [{"id": "9", "url": "http://b/9.jpg", "alt": "B9"}], calls=calls),
        ])
        t = time.monotonic()
        ok, _, _, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[])
        assert ok and tag == "b:9" and calls == ["a", "b"]
        assert time.monotonic() - t < 1.0            # 沒等 HEDGE_SECS

    def test_primary_error_opens_next_immediately(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", error=True),
            _src("b", [{"id": "9", "url": "http://b/9.jpg", "alt": "B9"}]),
        ])
        ok, _, _, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[])
        assert ok and tag == "b:9"

    def test_slow_primary_loses_to_fast_secondary(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "HEDGE_SECS", 0.3)
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", [{"id": "1", "url": "http://a/1.jpg", "alt": "A1"}], delay=2.0),
            _src("b", [{"id": "9", "url": "http://b/9.jpg", "alt": "B9"}]),
        ])
        t = time.monotonic()
        ok, _, _, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[])
        assert ok and tag == "b:9"                   # 誰先有圖就用誰
        assert time.monotonic() - t < 1.5            # 不等慢的那家

    def test_slow_primary_still_wins_if_secondary_empty(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "HEDGE_SECS", 0.2)
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", [{"id": "1", "url": "http://a/1.jpg", "alt": "A1"}], delay=0.6),
            _src("b", []),
        ])
        ok, _, _, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[])
        assert ok and tag == "a:1"

    def test_download_failure_falls_through_to_next_source(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", [{"id": str(i), "url": f"http://a/bad{i}.jpg", "alt": ""} for i in range(7)]),
            _src("b", [{"id": "9", "url": "http://b/9.jpg", "alt": "B9"}]),
        ])
        ok, _, _, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[])
        assert ok and tag == "b:9"

    def test_rejected_photo_skipped_same_source_next_photo(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", [{"id": "1", "url": "http://a/1.jpg", "alt": "A1"}, {"id": "2", "url": "http://a/2.jpg", "alt": "A2"}]),
        ])
        ok, _, desc, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=["a:1"])
        assert ok and tag == "a:2" and desc == "A2"

    def test_rejected_source_goes_last(self, tmp_path, monkeypatch, fast_download):
        calls = []
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", [{"id": "1", "url": "http://a/1.jpg", "alt": "A1"}], calls=calls),
            _src("b", [{"id": "9", "url": "http://b/9.jpg", "alt": "B9"}], calls=calls),
        ])
        ok, _, _, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=["a:1"])
        assert ok and tag == "b:9" and calls == ["b"]

    def test_all_empty_returns_four_falsy(self, tmp_path, monkeypatch, fast_download):
        monkeypatch.setattr(img, "SOURCES", [_src("a", []), _src("b", error=True)])
        assert img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[]) == (False, "", "", "")
        assert not (tmp_path / "o.jpg").exists()

    def test_reads_rejects_file_when_not_given(self, tmp_path, monkeypatch, fast_download):
        p = tmp_path / "rej.json"; p.write_text(json.dumps({"w": ["a:1"]}))
        monkeypatch.setattr(img, "REJECTS_PATH", str(p))
        monkeypatch.setattr(img, "SOURCES", [
            _src("a", [{"id": "1", "url": "http://a/1.jpg", "alt": ""}, {"id": "2", "url": "http://a/2.jpg", "alt": ""}]),
        ])
        assert img.fetch_image("W", str(tmp_path / "o.jpg"), "q")[3] == "a:2"


class TestDownloadUrlsAndSize:
    def _fake(self, monkeypatch, plan):
        """plan: url → (status, bytes 長度)；記錄呼叫順序。"""
        seen = []
        def fake_get(url, timeout=None, headers=None):
            seen.append(url)
            status, n = plan[url]
            return MagicMock(status_code=status, content=b"x" * n)
        monkeypatch.setattr(img.requests, "get", fake_get)
        return seen

    def test_thumbnail_424_falls_back_to_original_in_one_try(self, tmp_path, monkeypatch):
        monkeypatch.setattr(img, "MAX_DOWNLOAD_TRIES", 1)       # 只准試一張 → 證明備用網址不另外計次
        seen = self._fake(monkeypatch, {"http://o/t1": (424, 0), "http://o/f1": (200, 6000),
                                        "http://o/t2": (200, 6000)})
        monkeypatch.setattr(img, "SOURCES", [_src("o", [
            {"id": "1", "url": "http://o/t1", "urls": ["http://o/t1", "http://o/f1"], "alt": "A"},
            {"id": "2", "url": "http://o/t2", "urls": ["http://o/t2"], "alt": "B"}])])
        ok, _, desc, tag = img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[])
        assert (ok, tag, desc) == (True, "o:1", "A")
        assert seen == ["http://o/t1", "http://o/f1"]            # 第二張完全沒被要

    def test_oversized_skipped_next_candidate_chosen(self, tmp_path, monkeypatch):
        seen = self._fake(monkeypatch, {"http://a/big": (200, 2_000_000), "http://a/ok": (200, 6000)})
        monkeypatch.setattr(img, "SOURCES", [_src("a", [
            {"id": "1", "url": "http://a/big", "alt": ""}, {"id": "2", "url": "http://a/ok", "alt": ""}])])
        assert img.fetch_image("w", str(tmp_path / "o.jpg"), "q", rejects=[])[3] == "a:2"
        assert (tmp_path / "o.jpg").stat().st_size == 6000
