"""Image search CLI — called by Anki addon as subprocess.
stdout 協定（addon `Worker._fetch_image` 解析）：
  ALT: <照片描述>            有描述才印
  ATTRIBUTION: <署名 HTML>   有署名才印
  SOURCE: <來源:ID>          有圖必印（退圖辨識用）
失敗印 'FAIL: no usable image found' 並 exit 1。
帶 --sense（詞義）→ 走 core/picture.py 挑圖（LLM 挑＋看圖確認），這時 helper 會自己呼叫 LLM。"""
import os
import sys
import argparse
from core.llm import llm_image_query, engine_description
from core.image import fetch_image
from core.picture import find_picture


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
    parser.add_argument("--query", default="")
    parser.add_argument("--sense", default="")
    args = parser.parse_args()

    # addon 會帶 --query（搜圖關鍵字在 addon 的分流器上算）；CLI 直接跑才自己問 LLM
    query = args.query or llm_image_query(args.word, args.definition)
    print(f"QUERY: {query}" + ("" if args.query else f" [{engine_description()}]"), file=sys.stderr)

    if args.sense:
        found = find_picture(args.word, args.filepath, query, args.sense)
    else:
        found = fetch_image(args.word, args.filepath, search_query=query)
    ok, attribution, description, source = found
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
