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


def has_image(value):
    return "<img" in value
