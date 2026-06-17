import os
import requests

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

PEXELS_KEY_PATH = os.path.expanduser("~/Workspace/anki/.pexels_key")
PEXELS_API = "https://api.pexels.com/v1/search"


def _load_pexels_key():
    try:
        with open(PEXELS_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get("PEXELS_API_KEY", "")


def fetch_image(word, filepath, search_query=None):
    query = search_query or f"{word} meaning illustration"

    api_key = _load_pexels_key()
    if api_key:
        try:
            r = requests.get(PEXELS_API, params={"query": query, "per_page": 10},
                             headers={"Authorization": api_key}, timeout=10)
            if r.status_code == 200:
                for photo in r.json().get("photos", []):
                    url = photo.get("src", {}).get("large")
                    if not url:
                        continue
                    try:
                        img = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                        if img.status_code == 200 and len(img.content) > 5000:
                            with open(filepath, "wb") as f:
                                f.write(img.content)
                            photographer = photo.get("photographer", "Unknown")
                            photo_url = photo.get("url", "https://www.pexels.com")
                            attribution = (
                                f'<div style="font-size:10px;color:#999;margin-top:2px">'
                                f'Photo by {photographer} on '
                                f'<a href="{photo_url}" style="color:#999">Pexels</a></div>'
                            )
                            return True, attribution
                    except Exception:
                        continue
        except Exception:
            pass

    with DDGS() as ddgs:
        for result in ddgs.images(query, max_results=10):
            try:
                r = requests.get(result["image"], timeout=8, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and len(r.content) > 5000:
                    with open(filepath, "wb") as f:
                        f.write(r.content)
                    return True, ""
            except Exception:
                continue
    return False, ""
