"""Image search CLI — called by Anki addon as subprocess."""
import sys
import argparse
from core.llm import llm_image_query, engine_description
from core.image import fetch_image, _load_pexels_key

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("word")
    parser.add_argument("filepath")
    parser.add_argument("--definition", default="")
    args = parser.parse_args()

    query = llm_image_query(args.word, args.definition)
    print(f"QUERY: {query} [{engine_description()}]", file=sys.stderr)

    api_key = _load_pexels_key()
    if api_key:
        ok, attribution, description = fetch_image(args.word, args.filepath, search_query=query)
        if ok:
            if description:
                print(f"ALT: {description}")
            if attribution:
                print(f"ATTRIBUTION: {attribution}")
            sys.exit(0)

    print("FAIL: no usable image found")
    sys.exit(1)


if __name__ == "__main__":
    main()
