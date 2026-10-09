"""好卡範例索引：造句前找意思相近的好卡（複習 ≥ MIN_REPS 次、沒紅旗、非測試卡），
把它們的「單字＋例句」放進造句 prompt 當範例（few-shot）。

自足：只用 stdlib、無 aqt、無相對匯入。Anki 直接 import；core（core/examples.py）與
tools/build_example_index.py 用檔案路徑載入 → 全專案只有這一份，不用 KEEP-IN-SYNC。
任何失敗都不擋造句：呼叫端拿到 [] 就照原本的 prompt 造。"""
import json
import math
import os
import re
import time
import urllib.error
import urllib.request

_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
EXAMPLES_PATH = os.path.join(_REPO, "example_index.json")     # gitignored
GEMINI_KEY_PATH = os.path.join(_REPO, ".gemini_key")
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768
EMBED_URL = ("https://generativelanguage.googleapis.com/v1beta/models/"
             "{model}:batchEmbedContents")
MIN_REPS = 3
EXAMPLE_COUNT = 3
CARD_QUERY = f'"deck:My Daily English" -flag:1 -tag:whiteforge_test prop:reps>={MIN_REPS}'
# KEEP-IN-SYNC: addon/_config.py::PLACEHOLDERS（本模組不能 import _config）
_PLACEHOLDERS = ("No example found", "please add manually", "is used in English",
                 "Please add an example")

EXAMPLES_BLOCK_TEMPLATE = (
    "\n\nHere are example cards this learner already studied and kept. Match their style: "
    "short, plain, one concrete situation. Do not copy their sentences or situations.\n"
    "{lines}"
)


def _strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").replace("&nbsp;", " ").strip()


def _key_text(word, association=""):
    return f"{word} ({association})" if association else word


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def load_gemini_key():
    try:
        with open(GEMINI_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get("GEMINI_API_KEY", "")


def load_index(path=None):
    try:
        with open(path or EXAMPLES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, ValueError, OSError):
        return []


def save_index(entries, path=None):
    target = path or EXAMPLES_PATH
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    os.replace(tmp, target)


def plan_refresh(index, cards):
    """(留下的索引項, 要新算向量的卡)。例句改了、卡不再合格 → 移出；新合格 → 加入。"""
    by_id = {c["note_id"]: c for c in cards}
    keep = [e for e in index
            if e["note_id"] in by_id and by_id[e["note_id"]]["sentence"] == e["sentence"]]
    kept = {e["note_id"] for e in keep}
    return keep, [c for c in cards if c["note_id"] not in kept]


def embed(texts, key, timeout=30, retries=0):
    """批次 100 筆會撞每分鐘配額（429）→ 建索引時 retries>0：等一下重送同一批。"""
    out = []
    for i in range(0, len(texts), 100):
        batch = texts[i:i + 100]
        payload = json.dumps({"requests": [
            {"model": f"models/{EMBED_MODEL}", "content": {"parts": [{"text": t}]},
             "taskType": "SEMANTIC_SIMILARITY", "outputDimensionality": EMBED_DIMS}
            for t in batch]}).encode()
        req = urllib.request.Request(EMBED_URL.format(model=EMBED_MODEL), data=payload,
                                     headers={"Content-Type": "application/json",
                                              "x-goog-api-key": key})
        for attempt in range(retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = json.loads(r.read().decode())
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == retries:
                    raise
                time.sleep(30)
        out.extend(e["values"] for e in data["embeddings"])
    return out


def anki_connect(url):
    def call(action, **params):
        req = urllib.request.Request(url, data=json.dumps(
            {"action": action, "version": 6, "params": params}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode())
        if resp.get("error"):
            raise RuntimeError(f"AnkiConnect: {resp['error']}")
        return resp["result"]
    return call


def _good_cards(anki):
    cids = anki("findCards", query=CARD_QUERY)
    nids = sorted({c["note"] for c in anki("cardsInfo", cards=cids)}) if cids else []
    cards = []
    for n in (anki("notesInfo", notes=nids) if nids else []):
        f = n["fields"]
        sentence = _strip_html(f.get("Sentence", {}).get("value", ""))
        if not sentence or any(p in sentence for p in _PLACEHOLDERS):
            continue
        cards.append({"note_id": n["noteId"],
                      "word": _strip_html(f.get("Front", {}).get("value", "")).lower(),
                      "association": _strip_html(f.get("Association", {}).get("value", "")),
                      "sentence": sentence})
    return cards


def refresh_index(anki, key=None, path=None, retries=0):
    """增量更新索引。retries 預設 0（⌘S 用，快速失敗不睡覺）；建索引工具傳 >0。
    每批向量算完就存檔：中途失敗時，已算好的批次與留下的舊項目都保留，例外照常往上丟。"""
    key = key if key is not None else load_gemini_key()
    index = load_index(path)
    keep, to_add = plan_refresh(index, _good_cards(anki))
    removed = len(index) - len(keep)
    added = 0
    try:
        if key:
            for i in range(0, len(to_add), 100):
                chunk = to_add[i:i + 100]
                vecs = embed([_key_text(c["word"], c["association"]) for c in chunk], key,
                             retries=retries)
                keep += [{"note_id": c["note_id"], "word": c["word"], "sentence": c["sentence"],
                          "vector": v} for c, v in zip(chunk, vecs)]
                added += len(chunk)
                save_index(keep, path)
    finally:
        if removed and not added:   # 有算出新批次時，每批存檔已連同移除一起寫入
            save_index(keep, path)
    return added, removed


def nearest(index, qvec, k=EXAMPLE_COUNT, exclude_word=""):
    scored = sorted(((cosine(qvec, e["vector"]), e) for e in index
                     if e["word"] != exclude_word), key=lambda t: t[0], reverse=True)
    return [(e["word"], e["sentence"]) for _, e in scored[:k]]


def examples_for(word, association="", key=None, path=None):
    try:
        word = word.lower()
        key = key if key is not None else load_gemini_key()
        index = load_index(path)
        if not key or not index:
            return []
        qvec = embed([_key_text(word, association)], key, timeout=10)[0]
        return nearest(index, qvec, exclude_word=word)
    except Exception:
        return []


def examples_block(examples):
    if not examples:
        return ""
    return EXAMPLES_BLOCK_TEMPLATE.format(
        lines="\n".join(f"- {w}: {s}" for w, s in examples))
