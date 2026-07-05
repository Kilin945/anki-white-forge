"""LLM providers with per-provider headroom limiters.

Groq 的額度從 response header 讀（x-ratelimit-*）；Gemini API 不回 rate-limit
header → 用本地 token bucket 自己數。兩者對外同介面：headroom() / reset_secs()，
讓 dispatcher 做容量感知路由時不用管數字怎麼來。
"""
import os
import re
import threading
import time


def _parse_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _parse_reset_secs(s):
    """Groq reset header → seconds. Handles '1m26.4s' / '185ms' / '2.5s' / '1h2m'."""
    if not s:
        return 0.0
    return sum(float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
               for num, unit in re.findall(r"([\d.]+)(ms|s|m|h)", s))


class HeaderLimiter:
    """Header-fed headroom（Groq）。headroom() 回 0.0–1.0：剩餘 token 佔每分鐘上限的
    比例；未知（沒打過）→ 1.0；低於 floor → 0.0（提前一步停，永不真撞 429）。"""

    def __init__(self, token_floor=1500):
        self._floor = token_floor
        self._lock = threading.Lock()
        self._remaining = None
        self._limit = None
        self._reset_at = 0.0

    def headroom(self):
        with self._lock:
            if self._remaining is None:
                return 1.0
            if time.monotonic() >= self._reset_at:
                return 1.0                      # 窗口已過 → 額度回滿
            if self._remaining < self._floor:
                return 0.0
            limit = self._limit or max(self._remaining, 1)
            return min(1.0, self._remaining / limit)

    def reset_secs(self):
        with self._lock:
            if self._remaining is None or self._remaining >= self._floor:
                return 0.0
            return max(0.0, self._reset_at - time.monotonic())

    def update(self, headers):
        if not headers:
            return
        rt = _parse_int(headers.get("x-ratelimit-remaining-tokens"))
        if rt is None:
            return
        with self._lock:
            self._remaining = rt
            self._limit = _parse_int(headers.get("x-ratelimit-limit-tokens")) or self._limit
            self._reset_at = time.monotonic() + _parse_reset_secs(
                headers.get("x-ratelimit-reset-tokens"))

    def mark_exhausted(self, retry_after):
        """429 撞到了 → 額度視為 0，retry_after 秒後恢復。"""
        with self._lock:
            self._remaining = 0
            self._reset_at = time.monotonic() + float(retry_after)


class LocalBucketLimiter:
    """本地估算 headroom（Gemini 沒有 rate-limit header）：固定窗口計數（非滾動 —— 從第一次
    呼叫起算，滿一個 window 就整個重置，非每次呼叫往後平移）。窗口邊界可能有突發流量：舊窗口
    快到底時打滿、新窗口一開又立刻打滿，短時間內等於雙倍配額。"""

    def __init__(self, per_minute, window_secs=60.0):
        self._quota = per_minute
        self._window = window_secs
        self._lock = threading.Lock()
        self._window_start = None
        self._used = 0
        self._exhausted_until = 0.0

    def _roll(self):
        now = time.monotonic()
        if self._window_start is None or now - self._window_start >= self._window:
            self._window_start = now
            self._used = 0

    def record_call(self):
        with self._lock:
            self._roll()
            self._used += 1

    def headroom(self):
        with self._lock:
            now = time.monotonic()
            if now < self._exhausted_until:
                return 0.0
            self._roll()
            return max(0.0, (self._quota - self._used) / self._quota)

    def reset_secs(self):
        with self._lock:
            now = time.monotonic()
            if now < self._exhausted_until:
                return self._exhausted_until - now
            self._roll()
            if self._used < self._quota:
                return 0.0
            return max(0.0, self._window_start + self._window - now)

    def mark_exhausted(self, retry_after):
        with self._lock:
            self._exhausted_until = time.monotonic() + float(retry_after)


import requests
from groq import Groq, RateLimitError

GROQ_KEY_PATH = os.path.expanduser("~/Workspace/anki/.groq_key")
GROQ_MODEL = "llama-3.3-70b-versatile"

GEMINI_KEY_PATH = os.path.expanduser("~/Workspace/anki/.gemini_key")
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_RPM = 15          # 免費層每分鐘請求數（2026-07 查自官方文件；變了改這裡）


class ProviderError(Exception):
    """Provider 呼叫失敗（非 429）。"""


class ProviderRateLimited(ProviderError):
    """Provider 回 429。retry_after = 幾秒後再試。"""
    def __init__(self, retry_after=30.0):
        super().__init__("rate limited")
        self.retry_after = float(retry_after)


# KEEP-IN-SYNC with addon/_llm_dispatch.py::_backoff_secs
def _backoff_secs(retry_after, consecutive):
    """連續 429 的指數退避:別輕信 provider 的 Retry-After(Groq 免費層在 TPM 窗口
    耗盡時仍回 2 秒,照等只會無限循環)。第 n 次連續 429 → max(retry_after, 2×2^(n-1)),
    上限 60 秒。成功後歸零由呼叫端負責。"""
    return min(60.0, max(float(retry_after), 2.0 * (2 ** (max(consecutive, 1) - 1))))


def _load_groq_client():
    try:
        with open(GROQ_KEY_PATH) as f:
            key = f.read().strip()
        if key:
            return Groq(api_key=key)
    except FileNotFoundError:
        pass
    env_key = os.environ.get("GROQ_API_KEY", "")
    if env_key:
        return Groq(api_key=env_key)
    return None


def _retry_after_from(exc, default=60):
    """Groq RateLimitError 的 Retry-After 秒數；拿不到用 default。"""
    try:
        raw = exc.response.headers.get("retry-after")
        secs = int(float(raw))
        return secs if secs > 0 else default
    except (AttributeError, TypeError, ValueError):
        return default


class GroqProvider:
    name = "groq"
    model = GROQ_MODEL

    def __init__(self, client):
        self._client = client
        self._limiter = HeaderLimiter()
        self._consec_429 = 0
        self._c429_lock = threading.Lock()

    @classmethod
    def load(cls):
        client = _load_groq_client()
        return cls(client) if client else None

    def generate(self, prompt, *, temperature=0.7, max_tokens=200):
        try:
            raw = self._client.chat.completions.with_raw_response.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            self._limiter.update(raw.headers)
            with self._c429_lock:
                self._consec_429 = 0
            return raw.parse().choices[0].message.content.strip()
        except RateLimitError as e:
            with self._c429_lock:
                self._consec_429 += 1
                secs = _backoff_secs(_retry_after_from(e), self._consec_429)
            self._limiter.mark_exhausted(secs)
            raise ProviderRateLimited(secs)
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(str(e))

    def headroom(self):
        return self._limiter.headroom()

    def reset_secs(self):
        return self._limiter.reset_secs()


def _extract_gemini_text(data):
    """generateContent 回應 → 文字；形狀不對回 ''（呼叫端視為失敗）。"""
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        return ""


def _gemini_retry_secs(body, default=30.0):
    """429 回應 body 裡的 retryDelay（如 '13s'）；拿不到用 default。"""
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', body or "")
    return float(m.group(1)) if m else default


class GeminiProvider:
    name = "gemini"
    model = GEMINI_MODEL

    def __init__(self, key):
        self._key = key
        self._limiter = LocalBucketLimiter(GEMINI_RPM)
        self._consec_429 = 0
        self._c429_lock = threading.Lock()

    @classmethod
    def load(cls):
        try:
            with open(GEMINI_KEY_PATH) as f:
                key = f.read().strip()
            if key:
                return cls(key)
        except FileNotFoundError:
            pass
        env_key = os.environ.get("GEMINI_API_KEY", "")
        return cls(env_key) if env_key else None

    def generate(self, prompt, *, temperature=0.7, max_tokens=200):
        url = GEMINI_URL.format(model=self.model)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": max_tokens},
        }
        self._limiter.record_call()
        try:
            r = requests.post(url, json=payload, timeout=30,
                              headers={"x-goog-api-key": self._key})
        except requests.RequestException as e:
            raise ProviderError(str(e))
        if r.status_code == 429:
            with self._c429_lock:
                self._consec_429 += 1
                secs = _backoff_secs(_gemini_retry_secs(r.text), self._consec_429)
            self._limiter.mark_exhausted(secs)
            raise ProviderRateLimited(secs)
        if r.status_code != 200:
            raise ProviderError(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise ProviderError("bad json")
        text = _extract_gemini_text(data)
        if not text:
            raise ProviderError("empty response")
        with self._c429_lock:
            self._consec_429 = 0
        return text

    def headroom(self):
        return self._limiter.headroom()

    def reset_secs(self):
        return self._limiter.reset_secs()
