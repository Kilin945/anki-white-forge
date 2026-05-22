"""Download the first usable image for a word. Pexels primary, DuckDuckGo fallback.
LLM: Groq (primary), Ollama (fallback) for image query generation."""
import sys
import os
import argparse
import requests

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

from groq import Groq

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "gemma4:26b"
GROQ_KEY_PATH = os.path.expanduser("~/Workspace/Anki/.groq_key")
GROQ_MODEL = "llama-3.3-70b-versatile"
PEXELS_KEY_PATH = os.path.expanduser("~/Workspace/Anki/.pexels_key")
PEXELS_API = "https://api.pexels.com/v1/search"


def _load_groq_client():
    try:
        with open(GROQ_KEY_PATH) as f:
            key = f.read().strip()
        if key:
            return Groq(api_key=key)
    except FileNotFoundError:
        pass
    env_key = os.environ.get("GROQ_API_KEY", "")
    if env_key:
        return Groq(api_key=env_key)
    return None

_groq_client = _load_groq_client()


def _load_pexels_key():
    try:
        with open(PEXELS_KEY_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return os.environ.get("PEXELS_API_KEY", "")


def groq_generate(prompt):
    if not _groq_client:
        return ""
    try:
        resp = _groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=100,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[groq error] {e}", file=sys.stderr)
        return ""


def ollama_generate(prompt):
    try:
        r = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=30,
        )
        return r.json().get("response", "").strip()
    except requests.ConnectionError:
        print("WARNING: Ollama is not running! Please start it: ollama serve", file=sys.stderr)
        return ""
    except Exception:
        return ""


def llm(prompt):
    result = groq_generate(prompt)
    if result:
        return result
    return ollama_generate(prompt)


def image_query(word, definition="", sentence=""):
    context_parts = [f'the English word "{word}"']
    if definition:
        context_parts.append(f'which means "{definition}"')
    if sentence:
        context_parts.append(f'used in the sentence: "{sentence}"')
    context = ", ".join(context_parts)

    prompt = (
        f"Given {context}, give me a short Google image search query (5-8 words max) "
        f"to find a photo that clearly shows what this word means visually. "
        f"Focus on the concrete, visual meaning. Output only the search query, nothing else."
    )
    result = llm(prompt)
    if result and len(result) > 3:
        return result.strip('"\'')
    if definition:
        return f"{word} {definition} photo"
    return f"{word} meaning illustration"


def _fetch_pexels(query, filepath, api_key):
    r = requests.get(
        PEXELS_API,
        params={"query": query, "per_page": 10},
        headers={"Authorization": api_key},
        timeout=10,
    )
    if r.status_code != 200:
        return False, ""

    photos = r.json().get("photos", [])
    for photo in photos:
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
                print(f"OK: {url} [pexels]")
                return True, attribution
        except Exception:
            continue
    return False, ""


def _fetch_ddgs(query, filepath):
    with DDGS() as ddgs:
        for result in ddgs.images(query, max_results=10):
            try:
                r = requests.get(
                    result["image"], timeout=8,
                    headers={"User-Agent": "Mozilla/5.0"},
                )
                if r.status_code == 200 and len(r.content) > 5000:
                    with open(filepath, "wb") as f:
                        f.write(r.content)
                    print(f"OK: {result['image']} [ddgs]")
                    return True
            except Exception:
                continue
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("word")
    parser.add_argument("filepath")
    parser.add_argument("--definition", default="")
    parser.add_argument("--sentence", default="")
    args = parser.parse_args()

    query = image_query(args.word, args.definition, args.sentence)
    engine = f"Groq" if _groq_client else "Ollama"
    print(f"QUERY: {query} [{engine}]", file=sys.stderr)

    api_key = _load_pexels_key()
    if api_key:
        ok, attribution = _fetch_pexels(query, args.filepath, api_key)
        if ok:
            if attribution:
                print(f"ATTRIBUTION: {attribution}")
            sys.exit(0)

    if _fetch_ddgs(query, args.filepath):
        sys.exit(0)

    print("FAIL: no usable image found")
    sys.exit(1)


if __name__ == "__main__":
    main()
