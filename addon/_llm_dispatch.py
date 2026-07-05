"""Addon 端 LLM 分流鏡像 — Groq+Gemini 容量感知路由、斷路器、failover。

KEEP-IN-SYNC with core/providers.py + core/dispatcher.py（addon 不能 import
core → 邏輯鏡像一份，改一邊要改另一邊）。刻意差異（addon 特有）：
- generate(...) 多收 timeout（addon 各呼叫點 timeout 不同：拼字 8s、翻譯 10-15s）
- Dispatcher.wall_secs()/resets()：⌘S 停批閘與撞限訊息用
- CircuitBreaker.would_allow()：唯讀窺看，不消費半開試探閘（wall_secs 預檢用）
- format_reset_summary()：撞限訊息的英文摘要

自足：只用 stdlib、無 aqt、無相對匯入 → 測試以 spec_from_file_location 直接載入。
"""
import json
import logging
import logging.handlers
import os
import re
import threading
import time
import urllib.error
import urllib.request

LOG_PATH = os.path.expanduser("~/Workspace/anki/logs/addon_llm.log")


def get_logger():
    """檔案 logger（1MB×3 輪替）。冪等：重複呼叫/重複載入模組不會疊 handler。"""
    logger = logging.getLogger("whiteforge.llm")
    if not logger.handlers:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000,
                                                 backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger


GROQ_KEY_PATH = os.path.expanduser("~/Workspace/anki/.groq_key")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

GEMINI_KEY_PATH = os.path.expanduser("~/Workspace/anki/.gemini_key")
GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_RPM = 15          # 免費層每分鐘請求數（2026-07 查自官方文件；變了改這裡）

USER_AGENT = "AnkiWordAdder/1.0"


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


def _parse_retry_after(headers, default=60):
    """429 Retry-After header → 秒數；拿不到用 default。"""
    raw = headers.get("Retry-After") if headers else None
    try:
        secs = int(float(raw))
        return secs if secs > 0 else default
    except (TypeError, ValueError):
        return default


def _load_key(path, env_var):
    try:
        with open(path) as f:
            key = f.read().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    return os.environ.get(env_var, "")


class HeaderLimiter:
    """Header-fed headroom（Groq）。headroom() 回 0.0–1.0：剩餘 token 佔每分鐘上限
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
                return 1.0
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
        with self._lock:
            self._remaining = 0
            self._reset_at = time.monotonic() + float(retry_after)


class LocalBucketLimiter:
    """本地估算 headroom（Gemini 沒有 rate-limit header）：固定窗口計數（自首次呼叫
    起算，滿 window_secs 歸零）。窗界爆量風險：邊界前後各打滿一輪會在短時間送出雙倍
    quota → 靠 429/mark_exhausted/斷路器優雅退場。"""

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
            if time.monotonic() < self._exhausted_until:
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


class CircuitBreaker:
    """三態斷路器：CLOSED →（連續失敗達 threshold）→ OPEN →（冷卻到期）→ HALF-OPEN
    （單筆試探閘 _probe_inflight：同一時間只放一筆試探，成功復位/失敗再 OPEN）。"""

    def __init__(self, threshold=3, cooldown_default=30.0):
        self._threshold = threshold
        self._cooldown_default = cooldown_default
        self._lock = threading.Lock()
        self._streak = 0
        self._open_until = 0.0
        self._probe_inflight = False

    def allows(self):
        with self._lock:
            now = time.monotonic()
            if now < self._open_until:
                return False
            if self._open_until > 0.0:          # 冷卻剛過 → half-open：只放一筆試探
                if self._probe_inflight:
                    return False
                self._probe_inflight = True
                return True
            return True                          # CLOSED

    def would_allow(self):
        """唯讀窺看（不消費試探閘）— wall_secs 這種預檢用。"""
        with self._lock:
            now = time.monotonic()
            if now < self._open_until:
                return False
            if self._open_until > 0.0 and self._probe_inflight:
                return False
            return True

    def open_remaining(self):
        with self._lock:
            return max(0.0, self._open_until - time.monotonic())

    def record_success(self):
        with self._lock:
            self._streak = 0
            self._open_until = 0.0
            self._probe_inflight = False

    def record_failure(self, cooldown=None):
        with self._lock:
            self._streak += 1
            self._probe_inflight = False
            if self._streak >= self._threshold:
                secs = cooldown if cooldown is not None else self._cooldown_default
                self._open_until = time.monotonic() + float(secs)


class ProviderError(Exception):
    """Provider 呼叫失敗（非 429）。"""


class ProviderRateLimited(ProviderError):
    """Provider 回 429。retry_after = 幾秒後再試。"""
    def __init__(self, retry_after=30.0):
        super().__init__("rate limited")
        self.retry_after = float(retry_after)


class GroqProvider:
    name = "groq"
    model = GROQ_MODEL

    def __init__(self, key):
        self._key = key
        self._limiter = HeaderLimiter()

    @classmethod
    def load(cls):
        key = _load_key(GROQ_KEY_PATH, "GROQ_API_KEY")
        return cls(key) if key else None

    def generate(self, prompt, *, temperature, max_tokens, timeout):
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }).encode()
        req = urllib.request.Request(GROQ_API_URL, data=payload,
                  headers={"Content-Type": "application/json",
                           "Authorization": f"Bearer {self._key}",
                           "User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                self._limiter.update(r.headers)
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                secs = _parse_retry_after(e.headers)
                self._limiter.mark_exhausted(secs)
                raise ProviderRateLimited(secs)
            raise ProviderError(f"HTTP {e.code}")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(str(e))
        try:
            return data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError("bad response shape")

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

    @classmethod
    def load(cls):
        key = _load_key(GEMINI_KEY_PATH, "GEMINI_API_KEY")
        return cls(key) if key else None

    def generate(self, prompt, *, temperature, max_tokens, timeout):
        payload = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature,
                                 "maxOutputTokens": max_tokens},
        }).encode()
        req = urllib.request.Request(GEMINI_URL.format(model=self.model), data=payload,
                  headers={"Content-Type": "application/json",
                           "x-goog-api-key": self._key})
        self._limiter.record_call()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                try:
                    body = e.read().decode()
                except Exception:
                    body = ""
                secs = _gemini_retry_secs(body)
                self._limiter.mark_exhausted(secs)
                raise ProviderRateLimited(secs)
            raise ProviderError(f"HTTP {e.code}")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(str(e))
        text = _extract_gemini_text(data)
        if not text:
            raise ProviderError("empty response")
        return text

    def headroom(self):
        return self._limiter.headroom()

    def reset_secs(self):
        return self._limiter.reset_secs()


class AllProvidersLimited(Exception):
    """所有 provider 都不可用（額度見底/熔斷/失敗）。resets: 每家幾秒後恢復。"""

    def __init__(self, resets):
        super().__init__("all providers limited")
        self.resets = dict(resets)
        self.soonest_reset = min(self.resets.values()) if self.resets else 60.0


class Dispatcher:
    """單一決策點：headroom 快照一次 → 降冪 → allows() 在真的要打之前才問 →
    失敗依序 failover。非預期例外也 record_failure（釋放試探閘）後 re-raise。"""

    def __init__(self, providers, threshold=3, cooldown_default=30.0):
        self.providers = list(providers)
        self._breakers = {p.name: CircuitBreaker(threshold, cooldown_default)
                          for p in self.providers}

    def resets(self):
        return {p.name: max(p.reset_secs(), self._breakers[p.name].open_remaining())
                for p in self.providers}

    def wall_secs(self):
        """⌘S 停批閘：任一家可用 → 0.0；全數不可用 → 最快恢復秒數。
        用 would_allow()（唯讀）預檢，不消費半開試探閘。"""
        for p in self.providers:
            if p.headroom() > 0.0 and self._breakers[p.name].would_allow():
                return 0.0
        r = self.resets()
        return min(r.values()) if r else 0.0

    def generate(self, prompt, *, temperature, max_tokens, timeout):
        ranked = sorted(((p.headroom(), p) for p in self.providers),
                        key=lambda t: t[0], reverse=True)     # headroom 快照一次
        for h, p in ranked:
            if h <= 0.0:
                continue
            if not self._breakers[p.name].allows():           # 攻擊前才取試探閘
                continue
            _log.debug("route → %s headroom=%.2f", p.name, h)
            try:
                text = p.generate(prompt, temperature=temperature,
                                  max_tokens=max_tokens, timeout=timeout)
                self._breakers[p.name].record_success()
                return text
            except ProviderRateLimited as e:
                self._breakers[p.name].record_failure(e.retry_after)
                _log.warning("%s 429 retry_after=%s", p.name, e.retry_after)
                if self._breakers[p.name].open_remaining() > 0:
                    _log.warning("breaker OPEN %s cooldown=%s", p.name, e.retry_after)
            except ProviderError as e:
                cooldown = getattr(e, "retry_after", None)
                self._breakers[p.name].record_failure(cooldown)
                _log.warning("%s failed (%s) → failover", p.name, e)
                if self._breakers[p.name].open_remaining() > 0:
                    _log.warning("breaker OPEN %s cooldown=%s", p.name, cooldown)
            except Exception:
                self._breakers[p.name].record_failure()   # 非預期例外也要釋放試探閘
                raise
        _log.warning("all providers limited resets=%s", self.resets())
        raise AllProvidersLimited(self.resets())


def format_reset_summary(resets):
    """撞限訊息的英文摘要：'Gemini resets in ~15s, Groq in ~40s'（依名稱排序求穩定）。"""
    parts = []
    for i, (name, secs) in enumerate(sorted(resets.items())):
        label = name.capitalize()
        parts.append(f"{label} resets in ~{int(secs)}s" if i == 0
                     else f"{label} in ~{int(secs)}s")
    return ", ".join(parts)


_log = get_logger()   # module logger — created once, used by Dispatcher above (late binding)
