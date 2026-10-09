"""看圖：下載下來的圖到底有沒有在表現這個詞義（Gemini 看圖）。

只看描述挑圖不準（2026-10-10 實測：concrete 挑到「art class」、harness 挑到馬具），
所以下載後讓看得到圖的模型確認。實測 15 張（9 張錯、6 張對）全判對。

回 True／False／None。None＝沒辦法看（沒金鑰、每個模型都撞限或掛掉）→ 呼叫端照收，不擋生成。
模型照 VISION_MODELS 順序試，撞 429／5xx 就冷卻那個模型、換下一個。
這些呼叫不經過 Dispatcher（它只管文字），但跟造句池共用同一批 Gemini 模型的額度。
"""
import base64
import sys
import threading
import time

import requests

from core.providers import GEMINI_KEY_PATH, GEMINI_URL, _extract_gemini_text

VISION_MODELS = ["gemini-3.6-flash", "gemini-3.7-flash", "gemini-3-flash-preview", "gemini-3.5-flash"]
COOLDOWN_SECS = 60          # 撞限或伺服器錯誤後，這個模型多久內不再試
TIMEOUT_SECS = 40

_cool_until = {}            # model → monotonic time
_lock = threading.Lock()


def vision_prompt(word, sense):
    return (
        f'Would this photo work as a memory picture for the English word "{word}" in this meaning: '
        f'"{sense}"? Say YES if it shows the thing, situation or scene this meaning is about. '
        f'Say NO if it shows a different meaning of the word, or something unrelated. '
        f'Answer only YES or NO.'
    )


def _mime(data):
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _load_key():
    try:
        with open(GEMINI_KEY_PATH) as f:
            return f.read().strip()
    except OSError:
        return ""


def _ask(model, key, prompt, data):
    body = {
        "contents": [{"parts": [
            {"inline_data": {"mime_type": _mime(data), "data": base64.b64encode(data).decode()}},
            {"text": prompt},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1200},   # 思考段落也算 token
    }
    r = requests.post(GEMINI_URL.format(model=model), json=body, timeout=TIMEOUT_SECS,
                      headers={"x-goog-api-key": key})
    if r.status_code != 200:
        return None, r.status_code
    return _extract_gemini_text(r.json()), 200


def vision_fits(word, sense, image_path):
    key = _load_key()
    if not key:
        return None
    try:
        with open(image_path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    prompt = vision_prompt(word, sense)
    for model in VISION_MODELS:
        with _lock:
            if _cool_until.get(model, 0) > time.monotonic():
                continue
        try:
            text, status = _ask(model, key, prompt, data)
        except (requests.RequestException, ValueError, KeyError):
            text, status = None, 0
        if text:
            answer = text.strip().upper()
            if answer.startswith("YES"):
                return True
            if answer.startswith("NO"):
                return False
            continue                     # 回了別的（不是 YES/NO）→ 換下一個模型問，不冷卻
        with _lock:
            _cool_until[model] = time.monotonic() + COOLDOWN_SECS
        print(f"  [vision] {model} unavailable (HTTP {status}) → next model", file=sys.stderr)
    return None
