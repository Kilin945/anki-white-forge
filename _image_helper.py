"""Image search CLI — called by Anki addon as subprocess.
stdout 協定（addon `Worker._fetch_image` 解析）：
  ALT: <照片描述>            有描述才印
  ATTRIBUTION: <署名 HTML>   有署名才印
  SOURCE: <來源:ID>          有圖必印（退圖辨識用）
失敗印 'FAIL: no usable image found' 並 exit 1。"""
import os
import sys
import argparse
from core.llm import llm_image_query, engine_description
from core.image import fetch_image


def _exit(code):
    # 用 os._exit：ThreadPoolExecutor 的慢執行緒會在直譯器結束時被 join，會白等最多 8 秒
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("word")
    parser.add_argument("filepath")
    parser.add_argument("--definition", default="")
    args = parser.parse_args()

    query = llm_image_query(args.word, args.definition)
    print(f"QUERY: {query} [{engine_description()}]", file=sys.stderr)

    ok, attribution, description, source = fetch_image(args.word, args.filepath, search_query=query)
    if ok:
        if description:
            print(f"ALT: {description}")
        if attribution:
            print(f"ATTRIBUTION: {attribution}")
        if source:
            print(f"SOURCE: {source}")
        _exit(0)

    print("FAIL: no usable image found")
    _exit(1)


if __name__ == "__main__":
    main()
