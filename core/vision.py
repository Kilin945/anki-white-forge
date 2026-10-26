"""看圖：下載下來的圖到底有沒有在表現這個詞義（Gemini 看圖）。

只看描述挑圖不準（2026-10-10 實測：concrete 挑到「art class」、harness 挑到馬具），
所以下載後讓看得到圖的模型確認。實測 15 張（9 張錯、6 張對）全判對。

回 True／False／None。None＝沒辦法看（沒金鑰、每個模型都撞限、逾時或掛掉）→ 呼叫端不收這張圖。
模型照 VISION_MODELS 順序試。只有 429（額度用完）冷卻那個模型；逾時、5xx 是單次的慢或忙，不冷卻，
換下一個模型；5xx 回得快，所有模型都問過還沒答案就停一下再問一輪（逾時的模型不再等第二次）。
2026-10-26 事故：Gemini 那天看一張圖要 18～21 秒，舊的 8 秒上限每張都逾時，又一逾時就冷卻 60 秒，
同一張卡後面的圖全部看不了，連 oven 都停卡。
這些呼叫不經過 Dispatcher（它只管文字），但跟造句池共用同一批 Gemini 模型的額度。
"""
import base64
import json
import os
import sys
import threading
import time

import requests

from core.image import progress
from core.providers import GEMINI_KEY_PATH, GEMINI_URL, _REPO, _extract_gemini_text, _gemini_429_secs

# lite：15 張測試集同樣全對、每張 1.4～5 秒（flash 2～35 秒），而且不吃造句池 flash 的每日額度
# （2026-10-10 看圖把 flash 的每日額度用光，造句只能一路 failover，一張卡拖到 1 分半）。
VISION_MODELS = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
TIMEOUT_SECS = 30           # 單次看圖最多等多久（2026-10-26 實測慢的時候 18～21 秒）
RETRY_PAUSE_SECS = 2        # 5xx 之後停多久再問第二輪
# 冷卻記在檔案裡：helper 每張卡都是新的子程序，記在記憶體會讓每張卡都再撞一次「今天額度用完」
COOLDOWN_PATH = os.path.join(_REPO, "logs", "vision_cooldown.json")

_lock = threading.Lock()


def _load_cooldown():
    try:
        with open(COOLDOWN_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _cool(model, secs):
    """model 在 secs 秒內不再試（牆上時間，跨子程序有效）。寫檔失敗就算了，不擋看圖。"""
    with _lock:
        data = _load_cooldown()
        data[model] = time.time() + secs
        try:
            os.makedirs(os.path.dirname(COOLDOWN_PATH), exist_ok=True)
            with open(COOLDOWN_PATH, "w") as f:
                json.dump(data, f)
        except OSError:
            pass


def _cooling(model):
    return _load_cooldown().get(model, 0) > time.time()


def vision_prompt(word, sense):
    return (
        f'Would this photo work as a memory picture for the English word "{word}" in this meaning: '
        f'"{sense}"? Say YES if it shows the thing, situation or scene this meaning is about. '
        f'Say NO if it shows a different meaning of the word, or something unrelated. '
        f'Answer only YES or NO.'
    )


def parse_answer(text):
    """看圖回覆 → True/False；看不懂的回覆回 None。"""
    head = (text or "").replace("*", "").strip().upper()    # 模型偶爾回 **YES**
    if head.startswith("YES"):
        return True
    if head.startswith("NO"):
        return False
    return None


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
        return None, r.status_code, r.text
    return _extract_gemini_text(r.json()), 200, ""


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
    models = list(VISION_MODELS)
    for round_no in range(2):
        busy = []                        # 這輪回 5xx 的模型，第二輪再問
        for model in models:
            if _cooling(model):
                continue
            progress(f"checking photo with {model}")
            try:
                text, status, err = _ask(model, key, prompt, data)
            except (requests.RequestException, ValueError, KeyError) as e:
                print(f"  [vision] {model} no answer ({type(e).__name__}) → next model", file=sys.stderr)
                continue
            if text:
                verdict = parse_answer(text)
                if verdict is not None:
                    return verdict
                continue                 # 回了別的（不是 YES/NO）→ 換下一個模型問，不冷卻
            if status == 429:            # 額度用完才冷卻；每日額度冷卻到重置
                secs = _gemini_429_secs(err, 1)
                _cool(model, secs)
                print(f"  [vision] {model} rate limited → cool {secs:.0f}s, next model", file=sys.stderr)
                continue
            if status == 400 and "API_KEY" not in (err or "").upper():
                # 檔案不是圖（圖源回了 HTML 之類）：換模型、重試都一樣，這張就當不對。
                # 當成「看不了」會讓畫面一直說「稍後再試」，其實永遠不會好（2026-10-26 qa 實測）
                print(f"  [vision] {model} cannot read this file (HTTP 400) → not a fit", file=sys.stderr)
                return False
            print(f"  [vision] {model} unavailable (HTTP {status}) → next model", file=sys.stderr)
            if status >= 500:
                busy.append(model)
        if not busy or round_no == 1:
            break
        models = busy
        time.sleep(RETRY_PAUSE_SECS)
    return None
