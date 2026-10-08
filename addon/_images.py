"""圖片欄位的讀寫（`<img>` 的 alt／data-source）與退圖紀錄。KEEP-IN-SYNC 對照 core/image.py、core/text.py。"""

import re
import json
import html
from . import _llm_dispatch as _lld
from . import _config

_log = _lld.get_logger()   # 批次/LLM 事件集中記錄到 logs/addon_llm.log（gitignored）


def _image_html(filename, description="", attribution="", source=""):
    """Image_Prompt 欄位的 HTML。照片描述存在 alt（⌘S 重造句子時讀回）；
    圖片來源存在 data-source（"來源:ID"，⌘F 清紅旗卡時記成退圖）。
    KEEP IN SYNC with core/text.py::image_html。"""
    desc = " ".join((description or "").split())
    alt = f' alt="{html.escape(desc, quote=True)}"' if desc else ""
    src = f' data-source="{html.escape(source, quote=True)}"' if source else ""
    return f'<img src="{filename}"{alt}{src}>' + (attribution or "")


def _image_alt(value):
    """第一個 <img> 的 alt（照片描述），沒有回 ''。舊卡的圖沒有 alt → ''。
    KEEP IN SYNC with core/text.py::image_alt。"""
    m = re.search(r'<img[^>]*\balt="([^"]*)"', value or "")
    return html.unescape(m.group(1)) if m else ""


def _image_source(value):
    """第一個 <img> 的 data-source（"來源:ID"）。舊卡沒有這個屬性 → 一律當 "pexels:"；
    沒有圖 → ''。KEEP IN SYNC with core/text.py::image_source。"""
    value = value or ""
    if "<img" not in value:
        return ""
    m = re.search(r'<img[^>]*\bdata-source="([^"]*)"', value)
    return html.unescape(m.group(1)) if m else "pexels:"


def _record_image_reject(word, tag, path=None):
    """把「這個字退掉了這張圖」記進 image_rejects.json：{"word": ["來源:ID", …]}。
    只在 ⌘F Clear Flagged 呼叫（紅旗 = 使用者明確說這張不行；清長句不記）。
    鍵小寫、去重；檔案壞掉當空的重建。tag 空（卡上沒圖）→ 不寫、回 False。
    寫檔失敗（唯讀、目錄不存在）只記 log 並回 False、不 raise：退圖紀錄是附帶的，不能中斷清空。"""
    if not tag:
        return False
    path = path or _config.IMAGE_REJECTS_PATH
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, ValueError):
        data = {}
    key = word.lower()
    tags = data.get(key)
    if not isinstance(tags, list):       # 值被改成字串之類 → 換成空 list 再記
        tags = data[key] = []
    if tag not in tags:
        tags.append(tag)
    try:
        with open(path, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=0)
    except OSError as e:
        _log.warning("image reject not recorded for %s: %s", word, e)
        return False
    return True
