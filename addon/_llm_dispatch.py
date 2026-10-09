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

# repo 根從自己的位置推 — addon 是 symlink 掛進 Anki 的 addons21,
# 所以要 realpath 才會落在 repo 而不是 symlink 所在的資料夾。
_REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LOG_PATH = os.path.join(_REPO, "logs", "addon_llm.log")


def get_logger():
    """檔案 logger（1MB×3 輪替）。冪等：重複呼叫/重複載入模組不會疊 handler。
    設定失敗(權限/唯讀…)絕不往外拋 — logging 是周邊功能,不能炸掉 addon 載入。"""
    logger = logging.getLogger("whiteforge.llm")
    if not logger.handlers:
        try:
            os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
            h = logging.handlers.RotatingFileHandler(LOG_PATH, maxBytes=1_000_000,
                                                     backupCount=3, encoding="utf-8")
            h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(h)
            logger.setLevel(logging.INFO)
        except OSError:
            logger.addHandler(logging.NullHandler())   # 退化:靜默,但 logger 仍可用
        logger.propagate = False
    return logger


GROQ_KEY_PATH = os.path.join(_REPO, ".groq_key")
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"

GEMINI_KEY_PATH = os.path.join(_REPO, ".gemini_key")
GEMINI_MODEL = "gemini-flash-latest"  # 浮動別名：模型換代不會 404（2026-08 llama-3.3/gemini-2.0 同日退役事故）
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_RPM = 15          # 免費層每分鐘請求數（2026-07 查自官方文件；變了改這裡）

GEMINI_FLASH_RPM = 5     # 2026-10-09 AI Studio 額度頁：Flash 系列每分鐘 5 次
GEMINI_LITE_RPM = 15     # 同頁：Flash-Lite 每分鐘 15 次

CLOUDFLARE_KEY_PATH = os.path.join(_REPO, ".cloudflare_key")
CLOUDFLARE_URL = "https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{model}"
# 2026-10-09 查自官方頁 developers.cloudflare.com/workers-ai/platform/pricing/：
# 免費 10,000 neuron／天，每日 00:00 UTC 重置；用完「操作會失敗」，官方頁未載明狀態碼與訊息
# （limits 頁也沒有），故 _cloudflare_daily 以關鍵字猜，實際用完時要回頭校正。
CLOUDFLARE_DAILY_NEURONS = 10000
CLOUDFLARE_NEURON_RESERVE = 300    # 剩這麼多就先停；一次造句實測 24–34 neuron

# (provider, model, 每分鐘上限)。排列＝headroom 同分時的優先序。模型退役只改這裡。
# KEEP-IN-SYNC: core/providers.py 的同名常數。Lite 不准進造句池（實測漏單字、選錯詞義）。
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

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # 留 \t \n \r


def clean_llm_text(text):
    """去掉 LLM 回覆裡的控制字元（實測 Groq 回過 'penguin on ice\\x00'）。換行與 tab 保留：
    句子守門靠換行擋洩漏。KEEP-IN-SYNC: core/dispatcher.py 與 addon/_llm_dispatch.py 各一份。"""
    return _CONTROL_CHAR_RE.sub("", text) if text else text


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


# KEEP-IN-SYNC with core/providers.py::_backoff_secs
def _backoff_secs(retry_after, consecutive):
    """連續 429 的指數退避:別輕信 provider 的 Retry-After(Groq 免費層在 TPM 窗口
    耗盡時仍回 2 秒,照等只會無限循環)。第 n 次連續 429 → max(retry_after, 2×2^(n-1)),
    上限 60 秒。成功後歸零由呼叫端負責。"""
    return min(60.0, max(float(retry_after), 2.0 * (2 ** (max(consecutive, 1) - 1))))


class GroqProvider:
    def __init__(self, key, model=GROQ_MODEL):
        self._key = key
        self.model = model
        self.name = f"groq:{model}"
        self._limiter = HeaderLimiter()
        self._consec_429 = 0
        self._c429_lock = threading.Lock()

    @classmethod
    def load(cls):
        key = _load_key(GROQ_KEY_PATH, "GROQ_API_KEY")
        return cls(key) if key else None

    def generate(self, prompt, *, temperature, max_tokens, timeout, effort="low"):
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": max_tokens + _reasoning_headroom(effort),
            "reasoning_effort": effort,
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
                with self._c429_lock:
                    self._consec_429 += 1
                    secs = _backoff_secs(_parse_retry_after(e.headers), self._consec_429)
                self._limiter.mark_exhausted(secs)
                raise ProviderRateLimited(secs)
            raise ProviderError(f"HTTP {e.code}")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(str(e))
        try:
            text = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError("bad response shape")
        with self._c429_lock:
            self._consec_429 = 0
        return text

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
    不套 60 秒上限，否則每分鐘都去撞一次已經死一整天的模型；其餘照指數退避。"""
    if "PerDay" in (body or ""):
        return _gemini_retry_secs(body, default=3600.0)
    return _backoff_secs(_gemini_retry_secs(body), consecutive)


class GeminiProvider:
    def __init__(self, key, model=GEMINI_MODEL, rpm=GEMINI_RPM):
        self._key = key
        self.model = model
        self.name = f"gemini:{model}"
        self._limiter = LocalBucketLimiter(rpm)
        self._consec_429 = 0
        self._c429_lock = threading.Lock()

    @classmethod
    def load(cls):
        key = _load_key(GEMINI_KEY_PATH, "GEMINI_API_KEY")
        return cls(key) if key else None

    def generate(self, prompt, *, temperature, max_tokens, timeout, effort="low"):
        config = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if not _is_lite(self.model):            # Lite 不思考：送 thinkingConfig 沒實測過，不送
            config["maxOutputTokens"] += _reasoning_headroom(effort)
            config["thinkingConfig"] = {"thinkingLevel": _thinking_level(effort)}
        payload = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                              "generationConfig": config}).encode()
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
                with self._c429_lock:
                    self._consec_429 += 1
                    secs = _gemini_429_secs(body, self._consec_429)
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
        with self._c429_lock:
            self._consec_429 = 0
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
    """依任務分池：每筆呼叫只在該任務的池裡挑 headroom 最大且斷路器放行的模型，
    失敗依序換池內下一個；池全滅 raise AllProvidersLimited（只帶該池的 reset）。
    傳 list 等於兩個任務共用同一串（舊介面）。"""

    def __init__(self, pools, threshold=3, cooldown_default=30.0):
        if not isinstance(pools, dict):
            pools = {"sentence": list(pools), "light": list(pools)}
        self.pools = {task: list(ps) for task, ps in pools.items()}
        seen = {}
        for ps in self.pools.values():
            for p in ps:
                seen.setdefault(p.name, p)
        self.providers = list(seen.values())
        self._breakers = {name: CircuitBreaker(threshold, cooldown_default) for name in seen}

    def _pool(self, task):
        return self.pools.get(task, self.providers)

    def _reset_of(self, p):
        return max(p.reset_secs(), self._breakers[p.name].open_remaining())

    def resets(self, task=None):
        ps = self.providers if task is None else self._pool(task)
        return {p.name: self._reset_of(p) for p in ps}

    def wall_secs(self, task=None):
        """⌘S 停批閘：指定的池（None＝每個池）只要有一個模型可用就不算牆；
        全滅 → 該池最快恢復秒數；多池取最大。would_allow() 唯讀，不消費試探閘。"""
        worst = 0.0
        for t in ([task] if task else list(self.pools)):
            ps = self._pool(t)
            if any(p.headroom() > 0.0 and self._breakers[p.name].would_allow() for p in ps):
                continue
            secs = [self._reset_of(p) for p in ps]
            worst = max(worst, min(secs) if secs else 0.0)
        return worst

    def generate(self, prompt, *, temperature, max_tokens, timeout, effort="low", task="light"):
        ranked = sorted(((p.headroom(), p) for p in self._pool(task)),
                        key=lambda t: t[0], reverse=True)     # headroom 快照一次；穩定排序：同分照池內順序
        for h, p in ranked:
            if h <= 0.0:
                continue
            if not self._breakers[p.name].allows():           # 攻擊前才取試探閘
                continue
            (_log.info if task == "sentence" else _log.debug)(
                "%s via %s headroom=%.2f", task, p.name, h)
            try:
                text = p.generate(prompt, temperature=temperature,
                                  max_tokens=max_tokens, timeout=timeout,
                                  effort=effort)
                self._breakers[p.name].record_success()
                return clean_llm_text(text)
            except ProviderRateLimited as e:
                self._breakers[p.name].record_failure(e.retry_after)
                _log.warning("%s 429 retry_after=%s", p.name, e.retry_after)
                # 印實際生效的冷卻,不是例外帶來的建議值(後者可能是 None,
                # record_failure 會 fallback 到 cooldown_default)
                opened = self._breakers[p.name].open_remaining()
                if opened > 0:
                    _log.warning("breaker OPEN %s cooldown=%.0fs", p.name, opened)
            except ProviderError as e:
                cooldown = getattr(e, "retry_after", None)
                self._breakers[p.name].record_failure(cooldown)
                _log.warning("%s failed (%s) → failover", p.name, e)
                opened = self._breakers[p.name].open_remaining()
                if opened > 0:
                    _log.warning("breaker OPEN %s cooldown=%.0fs", p.name, opened)
            except Exception:
                self._breakers[p.name].record_failure()   # 非預期例外也要釋放試探閘
                raise
        _log.warning("pool %s limited resets=%s", task, self.resets(task))
        raise AllProvidersLimited(self.resets(task))


def format_reset_summary(resets):
    """撞限訊息：'next model frees up in ~15s (gemini:gemini-3.5-flash)'。"""
    if not resets:
        return "no model available"
    name, secs = min(resets.items(), key=lambda kv: kv[1])
    return f"next model frees up in ~{int(secs)}s ({name})"


def _load_cloudflare(path):
    """`.cloudflare_key`：兩行 KEY=VALUE（CLOUDFLARE_ACCOUNT_ID／CLOUDFLARE_API_TOKEN）。
    缺檔、缺欄位 → 退回環境變數；仍缺 → None（不啟用 Cloudflare）。"""
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
    API 回「每日額度用完」才是準的（同帳號也供 cf-image.sh 產圖，那份消耗這裡看不到）。"""

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
    """錯誤內容是不是「每日額度用完」（Step 1 依官方錯誤訊息調整關鍵字）。"""
    low = (body or "").lower()
    return "neuron" in low or "daily" in low


class CloudflareProvider:
    def __init__(self, account, token, model, budget):
        self._account, self._token, self._budget = account, token, budget
        self.model = model
        self.name = f"cloudflare:{model}"
        self._consec_429 = 0
        self._c429_lock = threading.Lock()

    def generate(self, prompt, *, temperature, max_tokens, timeout, effort="low"):
        extra = _reasoning_headroom(effort) if "gpt-oss" in self.model else 0
        payload = json.dumps({"messages": [{"role": "user", "content": prompt}],
                              "temperature": temperature,
                              "max_tokens": max_tokens + extra}).encode()
        req = urllib.request.Request(
            CLOUDFLARE_URL.format(account=self._account, model=self.model), data=payload,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode()
            except Exception:
                body = ""
            if _cloudflare_daily(body):
                self._budget.mark_exhausted()
                raise ProviderRateLimited(self._budget.reset_secs())
            if e.code == 429:
                with self._c429_lock:
                    self._consec_429 += 1
                    secs = _backoff_secs(_parse_retry_after(e.headers), self._consec_429)
                raise ProviderRateLimited(secs)
            raise ProviderError(f"HTTP {e.code}")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(str(e))
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


def build_pools():
    """依 SENTENCE_POOL／LIGHT_POOL 建 provider；沒金鑰的家族略過。同 (家, 模型) 只建一個實例。"""
    groq = _load_key(GROQ_KEY_PATH, "GROQ_API_KEY")
    gemini = _load_key(GEMINI_KEY_PATH, "GEMINI_API_KEY")
    cf = _load_cloudflare(CLOUDFLARE_KEY_PATH)
    budget = NeuronBudget(CLOUDFLARE_DAILY_NEURONS, CLOUDFLARE_NEURON_RESERVE)
    made = {}

    def make(kind, model, rpm):
        if (kind, model) not in made:
            if kind == "groq" and groq:
                made[(kind, model)] = GroqProvider(groq, model)
            elif kind == "gemini" and gemini:
                made[(kind, model)] = GeminiProvider(gemini, model, rpm)
            elif kind == "cloudflare" and cf:
                made[(kind, model)] = CloudflareProvider(cf[0], cf[1], model, budget)
            else:
                made[(kind, model)] = None
        return made[(kind, model)]

    return {"sentence": [p for p in (make(*e) for e in SENTENCE_POOL) if p],
            "light": [p for p in (make(*e) for e in LIGHT_POOL) if p]}


_log = get_logger()   # module logger — created once, used by Dispatcher above (late binding)
