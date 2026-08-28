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


def has_image(value):
    return "<img" in value
