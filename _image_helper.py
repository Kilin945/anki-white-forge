"""Download the first usable image for a word via DuckDuckGo image search."""
import sys
import requests
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

word, filepath = sys.argv[1], sys.argv[2]

with DDGS() as ddgs:
    results = list(ddgs.images(f"{word} meaning illustration", max_results=5))

for result in results:
    try:
        r = requests.get(
            result["image"], timeout=8,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if r.status_code == 200 and len(r.content) > 2000:
            with open(filepath, "wb") as f:
                f.write(r.content)
            print(f"OK: {result['image']}")
            sys.exit(0)
    except Exception:
        continue

print("FAIL: no usable image found")
sys.exit(1)
