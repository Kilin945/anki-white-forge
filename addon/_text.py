"""純函式：句子守門、翻譯驗證、退回原因、術語檔讀寫。不碰 Qt、不碰網路，pytest 直接測。"""

import os
import re
import json
import html
import shutil
import urllib.parse
import threading
from . import _llm_dispatch as _lld
from . import _config
from ._config import DEFAULT_TRANSLATION_TERMS, FIELD_BOXES, MAX_SENTENCE_WORDS, PLACEHOLDERS, _FIELD_LABEL

_log = _lld.get_logger()   # 批次/LLM 事件集中記錄到 logs/addon_llm.log（gitignored）


def _sentence_reason_text(engine):
    """`_llm_sentence` 第二值 → 畫面短句：沒回（撞限／斷線）與回了垃圾分開講。"""
    return "no reply" if engine == "no-reply" else "no clean sentence"


def _reasons_text(reasons):
    """橘框旁的一小段：`Meaning: got "Linux"`，多個欄位用「; 」接。依 FIELD_BOXES 順序。
    「skipped: …」是跟著別的欄位失敗才跳過的（例句失敗 → 翻譯／句音全跳），框已經橘了，
    列尾只講根本原因，不把五段全列出來。"""
    if all(k in reasons for k, _ in FIELD_BOXES):
        return ""                                  # 五個全退＝LLM 整個沒回（撞限／斷線），狀態列會講，列尾不重複
    real = {k: v for k, v in reasons.items() if not v.startswith("skipped")}
    shown = real or reasons
    return "; ".join(f"{_FIELD_LABEL[k]}: {shown[k]}" for k, _ in FIELD_BOXES if k in shown)


def _clean_text(raw, *, lower=False):
    # strip only real HTML tags (`<tag ...>` / `</tag>`); leave literal `<`…`>` in content
    text = html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", raw)).replace("\xa0", " ").strip()
    return text.lower() if lower else text


def _image_filename(value):
    """First <img src="…"> filename in a field, or None (no image / leftover HTML)."""
    m = re.search(r'<img[^>]*\bsrc="([^"]+)"', value or "")
    return m.group(1) if m else None


PREVIEW_IMAGE_BOX = 260   # 預覽圖的外框（px）：直式、橫式都縮進這個正方形，保持比例
PREVIEW_TEXT_WIDTH = 340  # 右欄文字寬（px），超過就換行


def _fit_box(width, height, box=PREVIEW_IMAGE_BOX):
    """(寬, 高) 等比縮進 box×box；讀不到尺寸（0）回 (box, box)。"""
    if width <= 0 or height <= 0:
        return box, box
    scale = box / max(width, height)
    return round(width * scale), round(height * scale)


def _preview_html(sentence, sentence_cn, translation, image_path, image_size=(0, 0)):
    """⌘S 補完那列的浮動預覽（整列的 tooltip）：左欄圖、右欄例句／整句翻譯／單字翻譯，
    讓使用者一眼比對圖和文字是不是同一個意思。欄位是 HTML → 先去標籤再跳脫；空的顯示「—」。
    圖用 file URL（媒體資料夾路徑有空白）；image_size 是原圖 (寬, 高)，縮進 PREVIEW_IMAGE_BOX。"""
    def line(raw):
        return html.escape(_clean_text(raw or "")) or "—"
    text = (f"<p style='font-size:18px; font-weight:600'>{line(sentence)}</p>"
            f"<p style='font-size:16px'>{line(sentence_cn)}</p>"
            f"<p style='font-size:16px; color:#64748B'>{line(translation)}</p>")
    if not image_path:
        return f"<table><tr><td width='{PREVIEW_TEXT_WIDTH}'>{text}</td></tr></table>"
    w, h = _fit_box(*image_size)
    img = f'<img src="file://{urllib.parse.quote(image_path)}" width="{w}" height="{h}">'
    return (f"<table cellspacing='8'><tr><td>{img}</td>"
            f"<td width='{PREVIEW_TEXT_WIDTH}' valign='top'>{text}</td></tr></table>")


ENGLISH_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\- ]*")


_PHRASE_RE = re.compile(r"[A-Za-z][A-Za-z'\-]*(?: [A-Za-z][A-Za-z'\-]*)+")


def _clean_terms(terms):
    """小寫、去頭尾空白、去空字串、去重（保序）。"""
    out, seen = [], set()
    for t in terms:
        t = t.strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ⌘S 的 ThreadPoolExecutor 會並行記錄 → 整段讀改寫要鎖;寫檔另走 tmp + os.replace(原子)
_TERMS_LOCK = threading.RLock()


def _backup_bad_terms_file(target):
    """壞檔複製到 .bak(盡力而為,失敗忽略)。"""
    try:
        shutil.copyfile(target, target + ".bak")
    except OSError:
        pass


def load_translation_terms(path=None):
    """讀 translation_terms.json → {"terms": [...], "pending": [...]}。
    缺檔／壞 JSON／欄位型別錯一律退回預設清單，不崩潰。
    KEEP-IN-SYNC: core/llm.py::load_translation_terms。"""
    default = {"terms": list(DEFAULT_TRANSLATION_TERMS), "pending": []}
    target = path or _config.TRANSLATION_TERMS_PATH
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        _backup_bad_terms_file(target)
        return default
    terms, pending = (data.get("terms"), data.get("pending", [])) if isinstance(data, dict) else (None, None)
    if (not isinstance(terms, list) or not all(isinstance(t, str) for t in terms)
            or not isinstance(pending, list)
            or not all(isinstance(p, dict) and isinstance(p.get("term"), str) for p in pending)):
        _backup_bad_terms_file(target)             # 手改壞了:留一份 .bak,免得下次存檔直接蓋掉
        return default
    return {"terms": terms, "pending": pending}


def save_translation_terms(data, path=None):
    """寫檔（terms 去重小寫去空白；pending 以 term 去重）。失敗回 False。
    KEEP-IN-SYNC: core/llm.py::save_translation_terms。"""
    pending, seen = [], set()
    for p in data.get("pending", []):
        key = p["term"].strip().lower()
        if key and key not in seen:
            seen.add(key)
            pending.append({**p, "term": key})
    out = {"terms": _clean_terms(data.get("terms", [])), "pending": pending}
    target = path or _config.TRANSLATION_TERMS_PATH
    with _TERMS_LOCK:
        tmp = target + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            os.replace(tmp, target)                 # 原子取代:並行的 load 不會讀到半截檔
            return True
        except OSError as e:
            _log.warning("translation_terms.json 寫入失敗: %s", e)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False


def record_rejected_translation(word, translation, path=None):
    """被驗證丟掉的翻譯 → 把裡面連續 ≥2 個英文字的片語記成待審（pending）。
    回傳新加的 term 列表；沒有新片語就不寫檔。
    KEEP-IN-SYNC: core/llm.py::record_rejected_translation。"""
    with _TERMS_LOCK:                                   # 讀-改-寫整段互斥
        data = load_translation_terms(path)
        known = {t.strip().lower() for t in data["terms"]} | {p["term"] for p in data["pending"]}
        added = []
        for m in _PHRASE_RE.findall(translation or ""):
            term = m.strip("'-").lower()
            if term and " " in term and term not in known:
                known.add(term)
                added.append(term)
                data["pending"].append({"term": term, "word": word, "translation": translation})
        if added:
            save_translation_terms(data, path)
        return added


def _looks_like_chinese_translation(text, terms=None):
    """整句翻譯驗證：要有中文，且扣掉白名單術語後英文字 < 3（3+ 視為前言／英文散文）。
    terms 省略 → 每次重讀 translation_terms.json（不快取，⌘D 改完立即生效）。
    KEEP-IN-SYNC: core/llm.py::_looks_like_chinese_translation。"""
    if not text:
        return False
    if not re.search(r"[一-鿿]", text):                 # must contain Chinese
        return False
    if terms is None:
        terms = load_translation_terms()["terms"]
    terms = sorted((t for t in terms if t.strip()), key=len, reverse=True)   # 長的優先:unit test coverage 先於 unit test
    rest = text
    if terms:
        rest = re.sub("|".join(re.escape(t) for t in terms), " ", text, flags=re.IGNORECASE)
    return len(re.findall(r"[A-Za-z]{2,}", rest)) < 3  # 3+ English words = preamble/English prose;


                                                       # a single embedded term (concurrency, Microsoft…) is kept


def _looks_english(word):
    """True if plausibly English (letters + space / - / '); rejects CJK, digits, symbols.
    Shared charset gate for both ⌘A (add) and ⌘S (complete)."""
    return bool(ENGLISH_WORD_RE.fullmatch((word or "").strip()))


def _sentence_word_count(html_value):
    """例句欄 → 英文字數(去 HTML)。空句/佔位符回 0 — 那是「缺句」(⌘S 的事),
    不是「長句」,回 0 讓它永遠不超過門檻。"""
    text = _clean_text(html_value or "")
    if not text or any(p in text for p in PLACEHOLDERS) or \
            text.startswith("Please add an example sentence"):
        return 0
    return len(text.split())


def _clamp_length_threshold(raw, default=20):
    """Rebuild Long Sentences 的門檻輸入解析:空白/非數字→default,下限 1,無上限
    (填 999 掃不到東西是合理結果)。"""
    try:
        n = int(str(raw).strip())
    except (ValueError, TypeError):
        return default
    return max(1, n)


def _long_sentence_label(word, count):
    """清單項目樣式:transient(27)。"""
    return f"{word}({count})"


def _sentence_usable(sentence):
    """句子可不可以餵給依賴句意的下游（翻譯/搜圖/配音）。
    空句或佔位符 → False：下游全部跳過，等真句子生出來一起重做，
    避免「翻譯了佔位符」這類欄位彼此不一致的髒卡。
    KEEP-IN-SYNC: core/text.py::sentence_usable（addon 不能 import core）。"""
    return bool(sentence) and not any(p in sentence for p in PLACEHOLDERS)


def _sentence_acceptable(text):
    """剛生成的句子像不像一句例句 —— 擋 LLM 洩漏（思考過程 / 被回吐的 prompt）。
    只用「合規句子必然通過」的結構性條件，寧可漏擋也不誤殺真句子：
      1. 大寫字母開頭 —— 洩漏多半是從長文中段截斷，開頭是半句小寫
      2. 字數 <= MAX_SENTENCE_WORDS —— prompt 要 6-12 字，實際洩漏樣本 33/34 字
      3. 不含換行 —— 例句是單句；多段落必是解說或思考
    事故樣本（兩張真卡）：
      'cause to collapse/stop functioning) ... But wait, "brought down" is ...'
      "priority: 1. If 'precise' has a common usage in software engineering ..."
    兩者都非空、非佔位符，舊的 len>10 放行後被當成真句子寫進卡片 → 下游翻譯永遠
    被驗證擋掉，⌘S 重跑幾次都修不好（句子「看起來合法」所以不會重生）。
    KEEP-IN-SYNC: core/text.py::sentence_acceptable。"""
    text = (text or "").strip()
    if len(text) <= 10:
        return False
    if not text[0].isupper():
        return False
    if "\n" in text:
        return False
    return len(text.split()) <= MAX_SENTENCE_WORDS


def _sentence_has_word(word, text):
    """例句裡找得到單字嗎——規則跟 templates/back.html 的高亮一樣（子字串、不分大小寫）。
    只是偵測，不是退句子的門：沒有就重問一次（見 Worker._llm_sentence）。
    KEEP-IN-SYNC: core/text.py::sentence_has_word。"""
    w = _clean_text(word or "", lower=True)
    return bool(w) and w in (text or "").lower()


def _sentence_to_write(current, generated, word):
    """生成結果 → 該寫入 Sentence 的值；None = 不要寫（保住既有真句子）。
    - 生成成功 → 寫生成句
    - 生成失敗且既有值是空/佔位符 → 寫佔位符（維持原行為，讓卡片仍被掃到）
    - 生成失敗但既有值是真句子 → None（絕不用佔位符蓋掉真句子）
    第二次補卡的資料損毀事故（撞限失敗仍用佔位符蓋掉已生成的真句子）就是少了這一層守門；
    第一層防線是 `_on_run` 改讀最新欄位，這裡是即使欄位判斷有誤也絕不覆蓋真句子的第二層。"""
    if generated:
        return generated
    if not current or any(p in current for p in PLACEHOLDERS):
        return f"Please add an example sentence for '{word}'."
    return None


def _need_sentence_audio(existing_audio, sentence, sentence_was_rewritten):
    """句音要不要(重)生成:佔位符絕不配音(留空,等真句配對生成);
    句子這一輪被重寫 → 強制重生(舊音檔已不匹配);否則缺才補。"""
    if not sentence or any(p in sentence for p in PLACEHOLDERS) or \
            sentence.startswith("Please add an example sentence"):
        return False
    return (not existing_audio) or sentence_was_rewritten


# traceback 濃縮:整頁 stderr → 最關鍵的 1~2 行例外訊息。
# Python traceback 精華永遠在最下面,且例外行「頂格不縮排」;
# File/code/^^^^ 都縮排、框架句可辨識 → 過濾掉後取最後幾行即為病根。
_TB_FRAME_LINES = (
    "Traceback (most recent call last):",
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
)


def _key_error_lines(stderr, max_lines=2):
    """把 subprocess 的整頁 traceback 濃縮成最關鍵的例外行(給面板顯示)。
    過濾縮排的 File/code 行、^^^^/~~~~ 指示箭頭、...<N lines>... 省略標記、
    框架句;保留頂格的例外行(如 socket.gaierror: ...),取最後 max_lines 行。
    萃取不到(非 traceback 字串)→ 回最後一條非空行(截斷)。"""
    lines = [ln.rstrip() for ln in (stderr or "").splitlines()]
    exc_lines = []
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if ln[0].isspace():            # 縮排 → File / code / ^^^^ / ~~~~
            continue
        if set(s) <= set("^~"):        # 純指示箭頭(保險:萬一沒縮排)
            continue
        if s.startswith("...<") and s.endswith(">..."):   # ...<5 lines>...
            continue
        if s in _TB_FRAME_LINES:
            continue
        exc_lines.append(s)
    if exc_lines:
        return "\n".join(exc_lines[-max_lines:])
    tail = [ln.strip() for ln in lines if ln.strip()]     # fallback:最後一條非空行
    return tail[-1][:300] if tail else ""


def _accept_word_translation(word, reply):
    """Validate a word-translation reply. Accept: a Chinese gloss (<=8 漢字, not a sentence,
    not buried in English preamble), OR a short English proper-noun NAME that echoes the
    input word (e.g. word 'spring' -> 'Spring Boot', 'kafka' -> 'Apache Kafka'). Reject
    refusals / preambles / junk that do not echo the word (e.g. 'None', 'I cannot translate').
    Returns the accepted reply, or '' to reject.
    KEEP IN SYNC with core/llm.py::_accept_word_translation (addon cannot import core)."""
    reply = (reply or "").strip()
    if not reply:
        return ""
    if re.search(r"[一-鿿]", reply):                       # Chinese gloss
        if len(re.findall(r"[一-鿿]", reply)) > 8:          # too long -> a sentence, not a term
            return ""
        if len(re.findall(r"[A-Za-z]{2,}", reply)) >= 3:    # Chinese + lots of English -> preamble
            return ""
        return reply
    # no Chinese -> only valid as a short proper-noun name that echoes the word
    if len(re.findall(r"[A-Za-z]+", reply)) <= 3 and word.lower() in reply.lower():
        return reply
    return ""


def _short_reply(reply, limit=30):
    reply = (reply or "").strip().replace("\n", " ")
    return reply if len(reply) <= limit else reply[:limit - 1] + "…"


def _translation_reject_reason(word, reply):
    """`_accept_word_translation` 退掉時的「為什麼」→ (分類, 畫面短句)；收下則 ("", "")。
    分類給 log（grep 用）、短句給畫面（橘框旁一小段，細節看 log）。判斷規則本身不在這裡，
    只是把 _accept_word_translation 的每個 return "" 對應成一個名字。"""
    if _accept_word_translation(word, reply):
        return "", ""
    reply = (reply or "").strip()
    if not reply:
        return "no-reply", "no reply"
    if re.search(r"[一-鿿]", reply):
        if len(re.findall(r"[一-鿿]", reply)) > 8:
            return "too-long", "too long"                 # 引文在 log；畫面只放原因
        return "preamble", "not a term"
    return "english-not-the-word", f'got "{_short_reply(reply, 20)}"'   # 這個短，直接看到回了什麼最有用
