"""搜圖：多圖源主從切換（hedged request）。

圖源依優先順序排隊：Pexels → Wikimedia Commons → Openverse → Pixabay。
先問第一個；它 HEDGE_SECS 秒內沒回結果就「加開」下一個（前一個不取消），
回錯誤或 0 張則立刻開下一個。**誰先有圖就用誰**——不重試、不 sleep。
同時搜四家會讓每張卡都打四次 API，所以只在前一家慢或空的時候才往下開。

退圖：使用者在手機標紅旗 → ⌘F Clear Flagged 清空時，addon 把那張圖的來源記進
REJECTS_PATH（以單字為鍵）。下次替同一個字找圖：退過的圖源排到最後、退過的那張不再挑。
"""
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import requests

_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
PEXELS_KEY_PATH = os.path.join(_REPO, ".pexels_key")
PIXABAY_KEY_PATH = os.path.join(_REPO, ".pixabay_key")
REJECTS_PATH = os.path.join(_REPO, "image_rejects.json")   # KEEP-IN-SYNC: addon/_config.py::IMAGE_REJECTS_PATH
PEXELS_API = "https://api.pexels.com/v1/search"
WIKIMEDIA_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
PIXABAY_API = "https://pixabay.com/api/"

HEDGE_SECS = 2          # 前一家多久沒回結果就加開下一家（只算搜尋，不含下載）
SEARCH_TIMEOUT = 8      # 單一圖源搜尋的逾時；慢的那家可以晚到，晚到但先有圖照樣用
DOWNLOAD_TIMEOUT = 8
MAX_DOWNLOAD_TRIES = 5  # 每家最多試幾張（有的連結會失效或太小）
MIN_IMAGE_BYTES = 5000
MAX_IMAGE_BYTES = 1_500_000   # Wikimedia 縮圖 / Pixabay 有時是幾 MB 的 PNG → 太大當失敗
THUMB_WIDTH = 900       # Wikimedia 原圖常幾十 MB → 只抓縮圖
UA = {"User-Agent": "AnkiWordAdder/1.0 (personal flashcards)"}   # Wikimedia 要求帶 UA
_ATTR_STYLE = "font-size:10px;color:#999;margin-top:2px"


def _read_key(path, env):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get(env, "")


def _load_pexels_key():
    return _read_key(PEXELS_KEY_PATH, "PEXELS_API_KEY")


def _load_pixabay_key():
    return _read_key(PIXABAY_KEY_PATH, "PIXABAY_API_KEY")


def _attribution(text_html):
    return f'<div style="{_ATTR_STYLE}">{text_html}</div>'


def _link(url, label):
    return f'<a href="{html.escape(url, quote=True)}" style="color:#999">{label}</a>'


def _clean(text):
    return " ".join((text or "").split())


def _esc(text):
    """署名文字：先壓掉換行再 escape（_image_helper 把署名印成單行 stdout，換行會截斷）。"""
    return html.escape(_clean(text))


# ── 各圖源：query → [candidate]，candidate = {id, url, urls, alt, attribution} ─────
# urls = 依序嘗試的下載網址（url 是 urls[0]）；一個 candidate 不管幾個網址都只算一次嘗試。
# 拋例外或回 [] 都算「這家沒有」，由 fetch_image 換下一家。

def _search_pexels(query):
    key = _load_pexels_key()
    if not key:
        return []
    r = requests.get(PEXELS_API, params={"query": query, "per_page": 10},
                     headers={"Authorization": key}, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    out = []
    for p in r.json().get("photos", []):
        url = p.get("src", {}).get("large")
        if url:
            out.append({
                "id": str(p.get("id", "")), "url": url, "urls": [url], "alt": _clean(p.get("alt")),
                "attribution": _attribution(
                    f'Photo by {_esc(p.get("photographer") or "Unknown")} on '
                    f'{_link(p.get("url", "https://www.pexels.com"), "Pexels")}'),
            })
    return out


def _search_wikimedia(query):
    r = requests.get(WIKIMEDIA_API, params={
        "action": "query", "format": "json", "generator": "search",
        "gsrsearch": f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": 10,
        "prop": "imageinfo", "iiprop": "url", "iiurlwidth": THUMB_WIDTH,
    }, headers=UA, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    pages = sorted((r.json().get("query", {}).get("pages") or {}).values(),
                   key=lambda p: p.get("index", 0))      # generator 的 dict 不保證順序
    out = []
    for p in pages:
        info = (p.get("imageinfo") or [{}])[0]
        url = info.get("thumburl") or info.get("url")
        if url:
            title = re.sub(r"\.\w+$", "", p.get("title", "").removeprefix("File:"))
            out.append({
                "id": str(p.get("pageid", "")), "url": url, "urls": [url], "alt": _clean(title),
                "attribution": _attribution(
                    f'{_esc(title)} via {_link(info.get("descriptionurl", "https://commons.wikimedia.org"), "Wikimedia Commons")}'),
            })
    return out


def _search_openverse(query):
    # mature=true：使用者決定圖片不做內容過濾（見 CLAUDE.md）
    r = requests.get(OPENVERSE_API, params={"q": query, "page_size": 10, "mature": "true"},
                     headers=UA, timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    out = []
    for p in r.json().get("results", []):
        # Flickr 來源的 thumbnail 常回 424（縮圖產生失敗），原圖卻是 200 → 兩個都給、先縮圖後原圖
        urls = list(dict.fromkeys(u for u in (p.get("thumbnail"), p.get("url")) if u))
        if urls:
            creator = _esc(p.get("creator") or "Unknown")
            out.append({
                "id": str(p.get("id", "")), "url": urls[0], "urls": urls, "alt": _clean(p.get("title")),
                "attribution": _attribution(
                    f'By {creator} via {_link(p.get("foreign_landing_url") or "https://openverse.org", "Openverse")}'),
            })
    return out


def _search_pixabay(query):
    key = _load_pixabay_key()
    if not key:
        return []
    r = requests.get(PIXABAY_API, params={"key": key, "q": query[:100], "per_page": 10},
                     timeout=SEARCH_TIMEOUT)
    r.raise_for_status()
    out = []
    for p in r.json().get("hits", []):
        url = p.get("webformatURL") or p.get("largeImageURL")
        if url:
            out.append({
                "id": str(p.get("id", "")), "url": url, "urls": [url], "alt": _clean(p.get("tags")),
                "attribution": _attribution(
                    f'Image by {_esc(p.get("user") or "Unknown")} on '
                    f'{_link(p.get("pageURL", "https://pixabay.com"), "Pixabay")}'),
            })
    return out


# 優先順序 = 列表順序。fetch_image 每次呼叫時才讀，測試可以整個換掉。
SOURCES = [
    ("pexels", _search_pexels),
    ("wikimedia", _search_wikimedia),
    ("openverse", _search_openverse),
    ("pixabay", _search_pixabay),
]


# ── 退圖紀錄 ──────────────────────────────────────────────────────────────────
# 格式：{"word": ["pexels:123", "pexels:", ...]}；"source:" 表示知道圖源、不知道是哪一張
# （舊卡）。只要出現過某圖源就把它排到最後。

def load_rejects(word, path=None):
    try:
        with open(path or REJECTS_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return []
    if not isinstance(data, dict):       # 檔案被改成 list 之類 → 當沒有，別讓整個搜圖崩
        return []
    val = data.get(word.lower(), [])
    return list(val) if isinstance(val, list) else []


def order_sources(names, rejects):
    """退過的圖源排到最後，其餘維持原優先順序（sorted 是穩定排序）。"""
    bad = {r.split(":", 1)[0] for r in rejects}
    return sorted(names, key=lambda n: n in bad)


def _download(candidates, source, rejects, filepath):
    """依序試下載，跳過退過的那張。成功回 candidate，全失敗回 None。"""
    tried = 0
    for c in candidates:
        if f"{source}:{c['id']}" in rejects:
            continue
        if tried >= MAX_DOWNLOAD_TRIES:
            break
        tried += 1
        for url in c.get("urls") or [c["url"]]:     # 同一張的備用網址不另外算次數
            try:
                img = requests.get(url, timeout=DOWNLOAD_TIMEOUT, headers=UA)
                if img.status_code == 200 and MIN_IMAGE_BYTES < len(img.content) <= MAX_IMAGE_BYTES:
                    with open(filepath, "wb") as f:
                        f.write(img.content)
                    return c
            except Exception:
                continue
    return None


def _safe_search(fn, query):
    try:
        return fn(query) or []
    except Exception:
        return []


def fetch_image(word, filepath, search_query=None, rejects=None):
    """找一張圖寫到 filepath。回 (ok, attribution_html, description, source_tag)。
    source_tag 形如 "pexels:123"，存進 <img data-src> 供之後退圖辨識。"""
    query = search_query or f"{word} meaning illustration"
    if rejects is None:
        rejects = load_rejects(word)
    fns = dict(SOURCES)
    queue = order_sources([n for n, _ in SOURCES], rejects)

    pool = ThreadPoolExecutor(max_workers=len(queue) or 1)
    pending = {}                         # future → source name
    try:
        def launch():
            name = queue.pop(0)
            pending[pool.submit(_safe_search, fns[name], query)] = name

        launch()
        next_at = time.monotonic() + HEDGE_SECS
        while pending:
            timeout = max(0.0, next_at - time.monotonic()) if queue else None
            done, _ = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if not done:                 # 前一家 HEDGE_SECS 內沒回 → 加開下一家
                launch()
                next_at = time.monotonic() + HEDGE_SECS
                continue
            for fut in done:
                name = pending.pop(fut)
                hit = _download(fut.result(), name, rejects, filepath)
                if hit:
                    return True, hit["attribution"], hit["alt"], f"{name}:{hit['id']}"
            if queue:                    # 這家沒有 → 不等，立刻開下一家
                launch()
                next_at = time.monotonic() + HEDGE_SECS
        return False, "", "", ""
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # 還在跑的慢圖源不等它
