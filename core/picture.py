"""挑一張「真的在表現這個詞義」的圖（2026-10-10）。

舊做法搜到什麼拿什麼：concrete 配到生物課老師、instance 配到 1922 年電話雜誌。現在三步：
1. 詞義：一家一家，LLM 從描述排出前 3 張 → 同時下載、同時看圖（core/vision.py）→ 取第一張對的。
   這家都不對才換下一家（一張一張排隊太慢，難找的字要 9～13 秒）。
2. 日常義：都不對 → 用單字本身搜，同樣挑＋看，標準換成「這個字最常見的日常意思」。
   技術義找不到時退回日常義的圖是使用者同意的（concrete 放混凝土牆）。
3. 最後退路：還是沒有，而詞義是軟體概念 → 通用程式畫面（不挑）；否則不放圖。
看圖沒辦法看（沒金鑰、撞限、沒時間了）→ 照收描述挑的那張，不擋生成。

整段有時間上限 PICTURE_BUDGET_SECS（使用者決定，2026-10-10：一張卡曾經找圖 50 秒）。
正常第一家就挑到是 2～7 秒；時間快到就不再換家，保留 FALLBACK_RESERVE_SECS 給舊流程
（hedged 搜尋拿第一張）——圖一定要先有，不能跳過（使用者否決「先建卡、圖之後補」）。
"""
import os
import sys
import threading
import time

from core import image, vision
import core.llm as llm

GENERIC_TECH_QUERY = "programming code on a computer screen"
MAX_VISION_CHECKS = 6       # 每張卡最多看幾張圖
PICTURE_BUDGET_SECS = 10    # 整段找圖的上限（使用者決定 10 秒）
FALLBACK_RESERVE_SECS = 3   # 留給「時間到 → 退路拿第一張」的時間
JUDGE_TIMEOUT_SECS = 5      # 從描述挑圖，單次最多等多久
VISION_TIMEOUT_SECS = 8     # 看圖，單次最多等多久（含換模型）
IS_TECH_TIMEOUT_SECS = 2    # 時間到時問「是不是技術義」最多等多久


def everyday_sense(word):
    return f'the most common everyday meaning of "{word}", not a software meaning'


def _timed(fn, secs):
    """最多等 secs 秒；逾時回 None（工作留在背景執行緒，不等它，子程序結束時一起收掉）。"""
    if secs <= 0:
        return None
    box = []
    t = threading.Thread(target=lambda: box.append(fn()), daemon=True)
    t.start()
    t.join(secs)
    return box[0] if box else None


def _log(msg):
    print(f"  [picture] {msg}", file=sys.stderr, flush=True)


def find_picture(word, filepath, query, sense, rejects=None, budget=None):
    """回 (ok, attribution_html, description, source_tag)，同 image.fetch_image。"""
    if rejects is None:
        rejects = image.load_rejects(word)
    deadline = time.monotonic() + (PICTURE_BUDGET_SECS if budget is None else budget)
    stop_at = deadline - FALLBACK_RESERVE_SECS      # 過了這個時間就不再換家，改走舊流程
    checks = [MAX_VISION_CHECKS]
    checks_lock = threading.Lock()            # 同一家的 TOP_N 張同時看圖 → 計數要上鎖

    def left(limit):
        return min(limit, stop_at - time.monotonic())

    def stage(q, s):
        def judge(alts):
            pick = _timed(lambda: llm.llm_pick_image(word, s, alts), left(JUDGE_TIMEOUT_SECS))
            _log(f"{word}: pick {pick} from {len(alts)}")
            return pick

        def verify(path):
            with checks_lock:
                if checks[0] <= 0:
                    return None
                checks[0] -= 1
            ok = _timed(lambda: vision.vision_fits(word, s, path), left(VISION_TIMEOUT_SECS))
            _log(f"{word}: vision {ok} for {s!r}")
            return ok
        return image.fetch_judged(filepath, q, rejects, judge, verify,
                                  keep_going=lambda: time.monotonic() < stop_at)

    for q, s in ((query, sense), (word, everyday_sense(word))):
        if time.monotonic() >= stop_at:
            break
        found = stage(q, s)
        if found:
            return found
    if time.monotonic() >= stop_at:
        # 時間到：技術義放通用程式畫面（比隨便一張不相干的圖好），其餘拿這個搜尋字串的第一張
        tech = _timed(lambda: llm.llm_is_tech(word, sense), IS_TECH_TIMEOUT_SECS)
        q = GENERIC_TECH_QUERY if tech else query
        _log(f"{word}: out of time → first image for {q!r}")
        found = _timed(lambda: image.fetch_image(word, filepath, search_query=q, rejects=rejects),
                       max(deadline - time.monotonic(), FALLBACK_RESERVE_SECS))
        if found and found[0]:
            return found
    elif _timed(lambda: llm.llm_is_tech(word, sense), left(JUDGE_TIMEOUT_SECS)):
        return image.fetch_image(word, filepath, search_query=GENERIC_TECH_QUERY, rejects=rejects)
    if os.path.exists(filepath):        # 看圖說不對的那張還留在檔案裡 → 清掉，卡片不會引用它
        os.remove(filepath)
    return False, "", "", ""
