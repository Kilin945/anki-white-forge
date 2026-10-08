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


def sentence_has_word(word, text):
    """例句裡找得到單字嗎——規則**跟 templates/back.html 的高亮一樣**：單字是句子的子字串、
    不分大小寫（模板是 `new RegExp(word + "[a-z]*", "gi")`，所以 penguin 配得到 penguins，
    sweep 配不到 swept）。這只是「卡片高亮得到嗎」的偵測，不是退句子的門：量過 1105 張卡
    硬擋會誤殺 49 張（不規則動詞、片語拆開用），所以呼叫端是「沒有就重問一次」。
    KEEP-IN-SYNC: addon/__init__.py::_sentence_has_word。"""
    w = strip_html(word or "").lower()
    return bool(w) and w in (text or "").lower()


def has_image(value):
    return "<img" in value


def image_html(filename, description="", attribution="", source=""):
    """Image_Prompt 欄位的 HTML。照片描述存在 alt —— 之後 ⌘S 重造句子時從這裡讀回
    （不另開欄位）；圖片來源存在 data-source（"來源:ID"）—— ⌘F 清紅旗卡時記成退圖。
    KEEP IN SYNC with addon/__init__.py::_image_html。"""
    desc = " ".join((description or "").split())
    alt = f' alt="{html.escape(desc, quote=True)}"' if desc else ""
    src = f' data-source="{html.escape(source, quote=True)}"' if source else ""
    return f'<img src="{filename}"{alt}{src}>' + (attribution or "")


def image_alt(value):
    """第一個 <img> 的 alt（照片描述），沒有回 ''。舊卡的圖沒有 alt → ''。
    KEEP IN SYNC with addon/__init__.py::_image_alt。"""
    m = re.search(r'<img[^>]*\balt="([^"]*)"', value or "")
    return html.unescape(m.group(1)) if m else ""


def image_source(value):
    """第一個 <img> 的 data-source（"來源:ID"）。舊卡沒有這個屬性 → 一律當 "pexels:"
    （知道圖源、不知道哪一張）；沒有圖 → ''。
    KEEP IN SYNC with addon/__init__.py::_image_source。"""
    value = value or ""
    if "<img" not in value:
        return ""
    m = re.search(r'<img[^>]*\bdata-source="([^"]*)"', value)
    return html.unescape(m.group(1)) if m else "pexels:"
