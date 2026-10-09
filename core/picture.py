"""挑一張「真的在表現這個詞義」的圖（2026-10-10）。

舊做法搜到什麼拿什麼：concrete 配到生物課老師、instance 配到 1922 年電話雜誌。現在三步：
1. 詞義：一家一家，LLM 從描述挑一張 → 下載 → 看圖模型確認（core/vision.py）。不對換下一家。
2. 日常義：都不對 → 用單字本身搜，同樣挑＋看，標準換成「這個字最常見的日常意思」。
   技術義找不到時退回日常義的圖是使用者同意的（concrete 放混凝土牆）。
3. 最後退路：還是沒有，而詞義是軟體概念 → 通用程式畫面（不挑）；否則不放圖。
看圖沒辦法看（沒金鑰、撞限）→ 照收描述挑的那張，不擋生成。
"""
import os
import sys

from core import image, vision
import core.llm as llm

GENERIC_TECH_QUERY = "programming code on a computer screen"
MAX_VISION_CHECKS = 6       # 每張卡最多看幾張圖（額度與時間的上限）；用完之後描述挑的就照收


def everyday_sense(word):
    return f'the most common everyday meaning of "{word}", not a software meaning'


def find_picture(word, filepath, query, sense, rejects=None):
    """回 (ok, attribution_html, description, source_tag)，同 image.fetch_image。"""
    if rejects is None:
        rejects = image.load_rejects(word)
    budget = [MAX_VISION_CHECKS]

    def stage(q, s):
        def judge(alts):
            return llm.llm_pick_image(word, s, alts)

        def verify(path):
            if budget[0] <= 0:
                return None
            budget[0] -= 1
            ok = vision.vision_fits(word, s, path)
            print(f"  [picture] {word}: vision {ok} for {s!r}", file=sys.stderr)
            return ok
        return image.fetch_judged(filepath, q, rejects, judge, verify)

    for q, s in ((query, sense), (word, everyday_sense(word))):
        found = stage(q, s)
        if found:
            return found
    if llm.llm_is_tech(word, sense):
        return image.fetch_image(word, filepath, search_query=GENERIC_TECH_QUERY, rejects=rejects)
    if os.path.exists(filepath):        # 看圖說不對的那張還留在檔案裡 → 清掉，卡片不會引用它
        os.remove(filepath)
    return False, "", "", ""
