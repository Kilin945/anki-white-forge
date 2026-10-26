"""Image search CLI — called by Anki addon as subprocess.
stdout 協定（addon `Worker._fetch_image` 解析）：
  ALT: <照片描述>            有描述才印
  ATTRIBUTION: <署名 HTML>   有署名才印
  SOURCE: <來源:ID>          有圖必印（退圖辨識用）
失敗印 'FAIL: no usable image found' 並 exit 1；有圖沒看成（看圖服務慢或掛了）印
'FAIL: photo check unavailable'，addon 據此在畫面寫「稍後再試」。
一律走 core/picture.py 挑圖（LLM 從描述挑＋看圖確認，只收看圖說對的）。--sense 是詞義；沒帶就
在這裡選一次（llm_pick_sense），再沒有就用「最常見的日常意思」——2026-10-18 起沒有「不挑不看拿第一張」的路。"""
import os
import sys
import argparse
from core.llm import llm_image_query, llm_pick_sense, engine_description
from core.picture import find_picture, everyday_sense


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

    sense = args.sense or llm_pick_sense(args.word) or everyday_sense(args.word)
    # addon 會帶 --query（搜圖關鍵字在 addon 的分流器上算）；CLI 直接跑才自己問 LLM
    query = args.query or llm_image_query(args.word, args.definition or sense)
    print(f"QUERY: {query}" + ("" if args.query else f" [{engine_description()}]"), file=sys.stderr)

    status = {}
    ok, attribution, description, source = find_picture(args.word, args.filepath, query, sense, status=status)
    if ok:
        if description:
            print(f"ALT: {description}")
        if attribution:
            print(f"ATTRIBUTION: {attribution}")
        if source:
            print(f"SOURCE: {source}")
        _exit(0)

    print("FAIL: photo check unavailable" if status.get("vision") == "unavailable"
          else "FAIL: no usable image found")
    _exit(1)


if __name__ == "__main__":
    main()
