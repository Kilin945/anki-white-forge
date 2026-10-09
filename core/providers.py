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

_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
GROQ_KEY_PATH = os.path.join(_REPO, ".groq_key")
GROQ_MODEL = "openai/gpt-oss-120b"

GEMINI_KEY_PATH = os.path.join(_REPO, ".gemini_key")
GEMINI_MODEL = "gemini-flash-latest"  # 浮動別名：模型換代不會 404（2026-08 llama-3.3/gemini-2.0 同日退役事故）
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_RPM = 15          # 免費層每分鐘請求數（2026-07 查自官方文件；變了改這裡）

# KEEP-IN-SYNC: addon/_llm_dispatch.py 的同名常數（test_core_pools_match_addon 比對）
GEMINI_FLASH_RPM = 5     # 2026-10-09 AI Studio 額度頁：Flash 系列每分鐘 5 次
GEMINI_LITE_RPM = 15     # 同頁：Flash-Lite 每分鐘 15 次

CLOUDFLARE_KEY_PATH = os.path.join(_REPO, ".cloudflare_key")
CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
# 2026-10-09 查自官方頁：免費 10,000 neuron／天，每日 00:00 UTC 重置；用完「操作會失敗」，
# 官方頁未載明狀態碼與訊息，故 _cloudflare_daily 以關鍵字猜，實際用完時要回頭校正。
CLOUDFLARE_DAILY_NEURONS = 10000
CLOUDFLARE_NEURON_RESERVE = 300    # 剩這麼多就先停；一次造句實測 24–34 neuron

# (provider, model, 每分鐘上限)。排列＝headroom 同分時的優先序。模型退役只改這裡。
# KEEP-IN-SYNC: addon/_llm_dispatch.py 的同名常數。Lite 不准進造句池（實測漏單字、選錯詞義）。
SENTENCE_POOL = [
    ("groq", "openai/gpt-oss-120b", None),
    ("cloudflare", "@cf/openai/gpt-oss-120b", None),
    ("gemini", "gemini-3-flash-preview", GEMINI_FLASH_RPM),
    ("gemini", "gemini-3.5-flash", GEMINI_FLASH_RPM),
    ("gemini", "gemini-3.6-flash", GEMINI_FLASH_RPM),
    ("gemini", "gemini-3.7-flash", GEMINI_FLASH_RPM),
]
LIGHT_POOL = [
    ("groq", "openai/gpt-oss-20b", None),
    ("cloudflare", "@cf/meta/llama-3.3-70b-instruct-fp8-fast", None),
    ("gemini", "gemini-3.1-flash-lite", GEMINI_LITE_RPM),
    ("gemini", "gemini-3.5-flash-lite", GEMINI_LITE_RPM),
]

# 兩家現行模型都是思考型：思考 token 也算進 max_tokens/maxOutputTokens，
# 小預算（翻譯 32、拼字 12）會被思考吃光 → 正文空字串。呼叫端的 max_tokens
# 語意維持「正文預算」，送出時由 provider 加上思考餘裕（實測 low 思考約 60-100 token）。
# effort="medium"（造句用）思考較長 → 給更深的餘裕。
REASONING_HEADROOM = 512
REASONING_HEADROOM_DEEP = 1024


def _reasoning_headroom(effort):
    return REASONING_HEADROOM if effort == "low" else REASONING_HEADROOM_DEEP


def _thinking_level(effort):
    # Gemini 只有 low/high 兩檔（minimal 被 API 拒絕）→ low 以上一律 high
    return "low" if effort == "low" else "high"


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
    """KEEP-IN-SYNC: addon/_llm_dispatch.py::GroqProvider（core 走 SDK client、無 timeout）。"""

    def __init__(self, client, model=GROQ_MODEL):
        self._client = client
        self.model = model
        self.name = f"groq:{model}"
        self._limiter = HeaderLimiter()
        self._consec_429 = 0
        self._c429_lock = threading.Lock()

    @classmethod
    def load(cls):
        client = _load_groq_client()
        return cls(client) if client else None

    def generate(self, prompt, *, temperature=0.7, max_tokens=200, effort="low"):
        try:
            raw = self._client.chat.completions.with_raw_response.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens + _reasoning_headroom(effort),
                extra_body={"reasoning_effort": effort},
            )
            self._limiter.update(raw.headers)
            text = raw.parse().choices[0].message.content.strip()
            with self._c429_lock:
                self._consec_429 = 0
            return text
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
    """generateContent 回應 → 文字；形狀不對回 ''（呼叫端視為失敗）。
    thinkingConfig 開著時 parts 會夾帶思考段落（該 part 帶 "thought": true）——盲取
    parts[0] 會把模型的自言自語或被回吐的 prompt 當成答案。事故：Anki 卡片的
    Sentence 欄位存進 'cause to collapse/stop functioning) ... But wait,' 這種思考
    中段，看起來像合法句子（非空、非佔位符）→ 閘門放行 → 下游翻譯永遠被驗證擋掉，
    重跑幾次都修不好。只收非 thought 的 part。
    KEEP IN SYNC: core/providers.py 與 addon/_llm_dispatch.py 各一份。"""
    try:
        parts = data["candidates"][0]["content"]["parts"]
        texts = [p["text"] for p in parts
                 if not p.get("thought") and isinstance(p.get("text"), str)]
    except (KeyError, IndexError, TypeError):
        return ""
    return "\n".join(texts).strip()


def _gemini_retry_secs(body, default=30.0):
    """429 回應 body 裡的 retryDelay（如 '13s'）；拿不到用 default。"""
    m = re.search(r'"retryDelay"\s*:\s*"([\d.]+)s"', body or "")
    return float(m.group(1)) if m else default


def _is_lite(model):
    return "lite" in model


def _gemini_429_secs(body, consecutive):
    """每日額度用完（quotaId 含 PerDay）→ 照 retryDelay 等到重置（實測 65152 秒），
    不套 60 秒上限，否則每分鐘都去撞一次已經死一整天的模型；其餘照指數退避。
    KEEP-IN-SYNC: addon/_llm_dispatch.py::_gemini_429_secs"""
    if "PerDay" in (body or ""):
        return _gemini_retry_secs(body, default=3600.0)
    return _backoff_secs(_gemini_retry_secs(body), consecutive)


class GeminiProvider:
    """KEEP-IN-SYNC: addon/_llm_dispatch.py::GeminiProvider（core 走 requests、無 timeout 參數）。"""

    def __init__(self, key, model=GEMINI_MODEL, rpm=GEMINI_RPM):
        self._key = key
        self.model = model
        self.name = f"gemini:{model}"
        self._limiter = LocalBucketLimiter(rpm)
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

    def generate(self, prompt, *, temperature=0.7, max_tokens=200, effort="low"):
        url = GEMINI_URL.format(model=self.model)
        config = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if not _is_lite(self.model):            # Lite 不思考：送 thinkingConfig 沒實測過，不送
            config["maxOutputTokens"] += _reasoning_headroom(effort)
            config["thinkingConfig"] = {"thinkingLevel": _thinking_level(effort)}
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": config}
        self._limiter.record_call()
        try:
            r = requests.post(url, json=payload, timeout=30,
                              headers={"x-goog-api-key": self._key})
        except requests.RequestException as e:
            raise ProviderError(str(e))
        if r.status_code == 429:
            with self._c429_lock:
                self._consec_429 += 1
                secs = _gemini_429_secs(r.text, self._consec_429)
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


def _load_cloudflare(path):
    """`.cloudflare_key`：兩行 KEY=VALUE（CLOUDFLARE_ACCOUNT_ID／CLOUDFLARE_API_TOKEN）。
    缺檔、缺欄位 → 退回環境變數；仍缺 → None（不啟用 Cloudflare）。
    KEEP-IN-SYNC: addon/_llm_dispatch.py::_load_cloudflare"""
    kv = {}
    try:
        with open(path) as f:
            for line in f:
                if "=" in line:
                    k, v = line.split("=", 1)
                    kv[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    acct = kv.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    token = kv.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CLOUDFLARE_API_TOKEN", "")
    return (acct, token) if acct and token else None


class NeuronBudget:
    """Cloudflare 免費額度按 UTC 日計 neuron；同帳號的模型共用一個。本地累加只是提前避讓，
    API 回「每日額度用完」才是準的（同帳號也供 cf-image.sh 產圖，那份消耗這裡看不到）。
    KEEP-IN-SYNC: addon/_llm_dispatch.py::NeuronBudget"""

    def __init__(self, daily, reserve):
        self._daily, self._reserve = float(daily), float(reserve)
        self._lock = threading.Lock()
        self._day, self._used, self._dead_day = None, 0.0, None

    def _roll(self):
        day = int(time.time() // 86400)
        if day != self._day:
            self._day, self._used = day, 0.0

    def _blocked(self):
        return self._dead_day == self._day or self._daily - self._used <= self._reserve

    def record(self, neurons):
        with self._lock:
            self._roll()
            self._used += float(neurons or 0.0)

    def mark_exhausted(self):
        with self._lock:
            self._roll()
            self._dead_day = self._day

    def headroom(self):
        with self._lock:
            self._roll()
            return 0.0 if self._blocked() else (self._daily - self._used) / self._daily

    def reset_secs(self):
        with self._lock:
            self._roll()
            return 86400 - (time.time() % 86400) if self._blocked() else 0.0


def _cloudflare_daily(body):
    """錯誤內容是不是「每日額度用完」。KEEP-IN-SYNC: addon/_llm_dispatch.py::_cloudflare_daily"""
    low = (body or "").lower()
    return "neuron" in low or "daily" in low


class CloudflareProvider:
    """KEEP-IN-SYNC: addon/_llm_dispatch.py::CloudflareProvider（core 走 requests、無 timeout 參數）。"""

    def __init__(self, account, token, model, budget):
        self._account, self._token, self._budget = account, token, budget
        self.model = model
        self.name = f"cloudflare:{model}"
        self._consec_429 = 0
        self._c429_lock = threading.Lock()

    def generate(self, prompt, *, temperature=0.7, max_tokens=200, effort="low"):
        extra = _reasoning_headroom(effort) if "gpt-oss" in self.model else 0
        payload = {"messages": [{"role": "user", "content": prompt}],
                   "temperature": temperature, "max_tokens": max_tokens + extra}
        try:
            r = requests.post(
                CLOUDFLARE_URL.format(account=self._account, model=self.model),
                json=payload, timeout=30,
                headers={"Authorization": f"Bearer {self._token}"})
        except requests.RequestException as e:
            raise ProviderError(str(e))
        if r.status_code != 200:
            if _cloudflare_daily(r.text):
                self._budget.mark_exhausted()
                raise ProviderRateLimited(self._budget.reset_secs())
            if r.status_code == 429:
                with self._c429_lock:
                    self._consec_429 += 1
                    secs = _backoff_secs(_retry_after_hdr(r), self._consec_429)
                raise ProviderRateLimited(secs)
            raise ProviderError(f"HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise ProviderError("bad json")
        res = data.get("result") or {}
        text = res.get("response")
        if not isinstance(text, str):
            try:
                text = res["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                text = ""
        self._budget.record((res.get("usage") or {}).get("neurons"))
        text = (text or "").strip()
        if not text:
            raise ProviderError("empty response")
        with self._c429_lock:
            self._consec_429 = 0
        return text

    def headroom(self):
        return self._budget.headroom()

    def reset_secs(self):
        return self._budget.reset_secs()


def _retry_after_hdr(r, default=60):
    """requests 回應的 Retry-After 秒數；拿不到用 default。"""
    try:
        secs = int(float(r.headers.get("Retry-After")))
        return secs if secs > 0 else default
    except (AttributeError, TypeError, ValueError):
        return default


def build_pools():
    """依 SENTENCE_POOL／LIGHT_POOL 建 provider；沒金鑰的家族略過。同 (家, 模型) 只建一個實例。
    KEEP-IN-SYNC: addon/_llm_dispatch.py::build_pools"""
    groq = _load_groq_client()
    gemini = GeminiProvider.load()
    cf = _load_cloudflare(CLOUDFLARE_KEY_PATH)
    budget = NeuronBudget(CLOUDFLARE_DAILY_NEURONS, CLOUDFLARE_NEURON_RESERVE)
    made = {}

    def make(kind, model, rpm):
        if (kind, model) not in made:
            if kind == "groq" and groq:
                made[(kind, model)] = GroqProvider(groq, model)
            elif kind == "gemini" and gemini:
                made[(kind, model)] = GeminiProvider(gemini._key, model, rpm)
            elif kind == "cloudflare" and cf:
                made[(kind, model)] = CloudflareProvider(cf[0], cf[1], model, budget)
            else:
                made[(kind, model)] = None
        return made[(kind, model)]

    return {"sentence": [p for p in (make(*e) for e in SENTENCE_POOL) if p],
            "light": [p for p in (make(*e) for e in LIGHT_POOL) if p]}
