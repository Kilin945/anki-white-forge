"""好卡範例索引（addon/_examples.py）。stdlib-only → 檔案路徑載入，免網路。"""
import importlib.util
import io
import json
import pathlib
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "addon_examples", pathlib.Path(__file__).parent.parent / "addon" / "_examples.py")
ex = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ex)


def test_cosine():
    assert ex.cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert ex.cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert ex.cosine([0, 0], [1, 0]) == 0.0


def test_load_index_missing_returns_empty(tmp_path):
    assert ex.load_index(str(tmp_path / "nope.json")) == []


def test_load_index_corrupt_returns_empty(tmp_path):
    p = tmp_path / "idx.json"
    p.write_text('[{"note_id": 1, "word": "a", "sent')      # 寫到一半
    assert ex.load_index(str(p)) == []


def test_save_then_load_roundtrip(tmp_path):
    p = str(tmp_path / "idx.json")
    entries = [{"note_id": 1, "word": "a", "sentence": "A b c.", "vector": [0.1, 0.2]}]
    ex.save_index(entries, p)
    assert ex.load_index(p) == entries


def test_plan_refresh_keeps_unchanged_adds_new_drops_changed_and_gone():
    index = [
        {"note_id": 1, "word": "keep", "sentence": "Same.", "vector": [1]},
        {"note_id": 2, "word": "edit", "sentence": "Old.", "vector": [1]},
        {"note_id": 3, "word": "gone", "sentence": "Bye.", "vector": [1]},
    ]
    cards = [
        {"note_id": 1, "word": "keep", "association": "", "sentence": "Same."},
        {"note_id": 2, "word": "edit", "association": "", "sentence": "New."},
        {"note_id": 4, "word": "new", "association": "", "sentence": "Hi."},
    ]
    keep, to_add = ex.plan_refresh(index, cards)
    assert [e["note_id"] for e in keep] == [1]
    assert sorted(c["note_id"] for c in to_add) == [2, 4]


def test_nearest_orders_by_similarity_and_excludes_self():
    index = [
        {"note_id": 1, "word": "retry", "sentence": "R.", "vector": [1.0, 0.0]},
        {"note_id": 2, "word": "apple", "sentence": "A.", "vector": [0.0, 1.0]},
        {"note_id": 3, "word": "rollback", "sentence": "B.", "vector": [0.9, 0.1]},
        {"note_id": 4, "word": "idempotent", "sentence": "I.", "vector": [1.0, 0.0]},
    ]
    got = ex.nearest(index, [1.0, 0.0], k=2, exclude_word="idempotent")
    assert got == [("retry", "R."), ("rollback", "B.")]


def test_examples_block():
    assert ex.examples_block([]) == ""
    block = ex.examples_block([("retry", "We retry the call.")])
    assert "- retry: We retry the call." in block
    assert "Do not copy" in block


def test_examples_for_swallows_errors(tmp_path):
    with patch.object(ex, "embed", side_effect=RuntimeError("boom")):
        assert ex.examples_for("penguin", key="k", path=str(tmp_path / "i.json")) == []


def test_examples_for_without_key_or_index(tmp_path):
    assert ex.examples_for("penguin", key="", path=str(tmp_path / "i.json")) == []


def test_embed_batches_by_100():
    sizes = []

    def fake_urlopen(req, timeout):
        n = len(json.loads(req.data.decode())["requests"])
        sizes.append(n)
        body = json.dumps({"embeddings": [{"values": [0.0]} for _ in range(n)]}).encode()

        class R(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R(body)
    with patch.object(ex.urllib.request, "urlopen", fake_urlopen):
        out = ex.embed([f"w{i}" for i in range(250)], key="k")
    assert sizes == [100, 100, 50] and len(out) == 250


def test_refresh_index_with_fake_anki(tmp_path):
    p = str(tmp_path / "i.json")

    def anki(action, **params):
        if action == "findCards":
            return [11, 12]
        if action == "cardsInfo":
            return [{"cardId": 11, "note": 1}, {"cardId": 12, "note": 1}]
        if action == "notesInfo":
            return [{"noteId": 1, "fields": {"Front": {"value": "retry"},
                                             "Association": {"value": ""},
                                             "Sentence": {"value": "We <b>retry</b> it."}}}]
    with patch.object(ex, "embed", lambda texts, key, timeout=30, retries=0: [[1.0, 0.0] for _ in texts]):
        added, removed = ex.refresh_index(anki, key="k", path=p)
    assert (added, removed) == (1, 0)
    assert ex.load_index(p)[0]["sentence"] == "We retry it."      # HTML 去掉


def test_refresh_index_skips_placeholder_sentences(tmp_path):
    p = str(tmp_path / "i.json")

    def anki(action, **params):
        if action == "findCards":
            return [11, 12]
        if action == "cardsInfo":
            return [{"cardId": 11, "note": 1}, {"cardId": 12, "note": 2}]
        if action == "notesInfo":
            return [
                {"noteId": 1, "fields": {"Front": {"value": "retry"}, "Association": {"value": ""},
                                         "Sentence": {"value": "No example found"}}},
                {"noteId": 2, "fields": {"Front": {"value": "ok"}, "Association": {"value": ""},
                                         "Sentence": {"value": "It is ok."}}}]
    with patch.object(ex, "embed", lambda texts, key, timeout=30, retries=0: [[1.0] for _ in texts]):
        ex.refresh_index(anki, key="k", path=p)
    assert [e["note_id"] for e in ex.load_index(p)] == [2]


def test_placeholders_in_sync_with_config():
    import addon
    assert ex._PLACEHOLDERS == tuple(addon._config.PLACEHOLDERS)


def test_examples_for_success_and_case_insensitive_exclude(tmp_path):
    p = str(tmp_path / "i.json")
    ex.save_index([
        {"note_id": 1, "word": "penguin", "sentence": "P.", "vector": [1.0, 0.0]},
        {"note_id": 2, "word": "retry", "sentence": "R.", "vector": [0.9, 0.1]}], p)
    with patch.object(ex, "embed", lambda texts, key, timeout=30, retries=0: [[1.0, 0.0]]):
        assert ex.examples_for("Penguin", key="k", path=p) == [("retry", "R.")]


def _http429(code=429):
    return ex.urllib.error.HTTPError("u", code, "x", {}, None)


def _ok_resp(req):
    n = len(json.loads(req.data.decode())["requests"])

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R(json.dumps({"embeddings": [{"values": [0.0]} for _ in range(n)]}).encode())


def test_embed_retries_once_on_429(monkeypatch):
    sleeps, calls = [], []

    def fake(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise _http429()
        return _ok_resp(req)
    monkeypatch.setattr(ex.urllib.request, "urlopen", fake)
    monkeypatch.setattr(ex.time, "sleep", lambda s: sleeps.append(s))
    assert len(ex.embed(["a"], key="k", retries=2)) == 1
    assert len(calls) == 2 and len(sleeps) == 1


def test_embed_429_without_retries_raises_immediately(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ex.urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(_http429()))
    monkeypatch.setattr(ex.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(ex.urllib.error.HTTPError):
        ex.embed(["a"], key="k", retries=0)
    assert sleeps == []


def test_embed_non_429_raises_immediately_even_with_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ex.urllib.request, "urlopen",
                        lambda req, timeout: (_ for _ in ()).throw(_http429(500)))
    monkeypatch.setattr(ex.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(ex.urllib.error.HTTPError):
        ex.embed(["a"], key="k", retries=3)
    assert sleeps == []


def test_refresh_index_keeps_first_batch_when_second_fails(tmp_path):
    p = str(tmp_path / "i.json")
    ids = list(range(1, 151))

    def anki(action, **params):
        if action == "findCards":
            return ids
        if action == "cardsInfo":
            return [{"cardId": i, "note": i} for i in ids]
        if action == "notesInfo":
            return [{"noteId": i, "fields": {"Front": {"value": f"w{i}"},
                                             "Association": {"value": ""},
                                             "Sentence": {"value": f"Sentence {i}."}}}
                    for i in ids]
    n = {"c": 0}

    def fake_embed(texts, key, timeout=30, retries=0):
        n["c"] += 1
        if n["c"] == 2:
            raise RuntimeError("boom")
        return [[1.0, 0.0] for _ in texts]
    with patch.object(ex, "embed", fake_embed):
        with pytest.raises(RuntimeError):
            ex.refresh_index(anki, key="k", path=p)
    assert len(ex.load_index(p)) == 100
