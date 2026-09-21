import re
import html

PLACEHOLDERS = ["No example found", "please add manually", "is used in English", "Please add an example"]


def strip_html(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text)).replace("\xa0", " ").strip()


def normalize(text):
    return (text
        .replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace("\xa0", " ")
    )


def is_placeholder(text):
    return any(p in text for p in PLACEHOLDERS)


def sentence_usable(sentence):
    """句子可不可以餵給依賴句意的下游（翻譯/搜圖/配音）。
    空句或佔位符 → False：下游全部跳過，等真句子生出來一起重做，
    避免「翻譯了佔位符」這類欄位彼此不一致的髒卡。
    KEEP-IN-SYNC: addon/__init__.py::_sentence_usable（addon 不能 import core）。"""
    return bool(sentence) and not is_placeholder(sentence)


MAX_SENTENCE_WORDS = 25   # prompt 規格 6-12 字；放寬到 25 仍遠低於洩漏樣本(33/34 字)


def sentence_acceptable(text):
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
    KEEP-IN-SYNC: core/text.py::sentence_acceptable 與 addon/__init__.py::_sentence_acceptable。"""
    text = (text or "").strip()
    if len(text) <= 10:
        return False
    if not text[0].isupper():
        return False
    if "\n" in text:
        return False
    return len(text.split()) <= MAX_SENTENCE_WORDS


def has_image(value):
    return "<img" in value
