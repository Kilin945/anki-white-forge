"""挑一張「真的在表現這個詞義」的圖（2026-10-10；2026-10-18 改成寧缺勿濫）。

舊做法搜到什麼拿什麼：concrete 配到生物課老師、instance 配到 1922 年電話雜誌。現在兩輪：
1. 詞義：一家一家，LLM 從描述排出前 3 張 → 同時下載、同時看圖（core/vision.py）→ 取第一張看圖說「對」的。
   這家都不對才換下一家（一張一張排隊太慢，難找的字要 9～13 秒）。
2. 日常義：都不對 → 用單字本身搜，同樣挑＋看，標準換成「這個字最常見的日常意思」。
   技術義找不到時退回日常義的圖是使用者同意的（concrete 放混凝土牆）。

兩輪都沒有 → 沒圖（回 False）。**只有看圖明確回 YES 的圖才收**：看不了（沒金鑰、撞限、逾時）
一律當沒有；沒有「時間到拿第一張」、沒有「技術字放程式碼畫面」這種退路——2026-10-18 使用者決定：
寧願沒圖停卡，也不要一張沒看過的圖（當天 42 張卡裡最離譜的錯圖全是退路收的）。
整段也不再有時間上限（原本 10 秒，時間一到就走退路亂收）；單次挑圖／看圖仍各有逾時，
逾時就當這張看不了。沒圖時卡片怎麼處理見呼叫端（⌘A／⌘S／CLI：其他欄位都不生成，停卡等下次）。

沒圖分兩種，呼叫端給 status dict 才分得出來：有圖沒看成（看圖服務慢或掛了）→ status["vision"]＝
"unavailable"，畫面要說「稍後再試」；有看過、都說不對 → 不寫，就是真的沒圖。
2026-10-26：Gemini 變慢，連 oven 都停卡，畫面卻只寫「no image」，看不出是產線壞了。
"""
import os
import sys
import threading

from core import image, vision
import core.llm as llm

MAX_VISION_CHECKS = 6       # 每一輪（詞義／日常義）最多看幾張圖；看滿之後的圖都算看不了＝沒有
# 原本整張卡共用 6 張：第一輪看滿 6 張都說不對，第二輪挑出的圖一張都沒看就停卡（2026-10-26 inherit）
JUDGE_TIMEOUT_SECS = 15     # 從描述挑圖，單次最多等多久（逾時就拿前 3 張去看，不會因此沒圖）
# 看圖，單次最多等多久：每個模型最多問兩輪、每次最多 vision.TIMEOUT_SECS，中間停一下
VISION_TIMEOUT_SECS = 2 * len(vision.VISION_MODELS) * vision.TIMEOUT_SECS + vision.RETRY_PAUSE_SECS + 2


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


def find_picture(word, filepath, query, sense, rejects=None, status=None):
    """回 (ok, attribution_html, description, source_tag)。沒有看圖確認過的圖就回 (False, "", "", "")。
    status：呼叫端給的 dict；有圖沒看成時寫 status["vision"] = "unavailable"。"""
    if rejects is None:
        rejects = image.load_rejects(word)
    checks = [MAX_VISION_CHECKS]
    verdicts = []                             # 每次看圖的結果：True／False／None（沒看成）
    checks_lock = threading.Lock()            # 同一家的 TOP_N 張同時看圖 → 計數要上鎖

    def stage(q, s):
        checks[0] = MAX_VISION_CHECKS        # 每一輪各自的額度
        def judge(alts):
            image.progress(f"picking from {len(alts)} photos")
            pick = _timed(lambda: llm.llm_pick_image(word, s, alts), JUDGE_TIMEOUT_SECS)
            _log(f"{word}: pick {pick} from {len(alts)}")
            return pick

        def verify(path):
            with checks_lock:
                if checks[0] <= 0:
                    return None
                checks[0] -= 1
            ok = _timed(lambda: vision.vision_fits(word, s, path), VISION_TIMEOUT_SECS)
            verdicts.append(ok)
            _log(f"{word}: vision {ok} for {s!r}")
            return ok
        return image.fetch_judged(filepath, q, rejects, judge, verify)

    for q, s in ((query, sense), (word, everyday_sense(word))):
        found = stage(q, s)
        if found:
            return found
    unavailable = None in verdicts         # 有圖沒看成：那張可能就是對的，不能算「真的沒圖」
    if unavailable and status is not None:
        status["vision"] = "unavailable"
    _log(f"{word}: " + ("photo check unavailable" if unavailable
                        else "no picture passed the check") + " → no image")
    if os.path.exists(filepath):        # 看圖說不對的那張還留在檔案裡 → 清掉，卡片不會引用它
        os.remove(filepath)
    return False, "", "", ""
