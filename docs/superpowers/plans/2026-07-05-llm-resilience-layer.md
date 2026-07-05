# LLM 韌性層（Groq + Gemini 分流）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** core 的 LLM 呼叫從「單打 Groq」變成「Groq + Gemini active-active 分流」：容量感知路由、斷路器、failover；兩家見底時帶真實 reset 時間回報。

**Architecture:** 新增 `core/providers.py`（兩個 provider，各帶 headroom limiter：Groq 讀 header、Gemini 本地 bucket）與 `core/dispatcher.py`（路由＋斷路器＋failover）。切入點在 `core/llm.py::groq_generate(_strict)` 內部——既有呼叫端介面不變，strict 版把新例外翻譯成既有 `RateLimitReached`。

**Tech Stack:** Python 3.13、pytest（假時鐘 `patch time.monotonic`）、groq SDK（`with_raw_response` 取 header）、requests（Gemini REST）。

**Spec:** `docs/superpowers/specs/2026-07-05-llm-resilience-layer-design.md`

## Global Constraints

- 測試不打真網路：provider 用假物件注入 dispatcher；時間用假時鐘。
- 既有 104 個測試必須保持綠（尤其 `test_backfill.py` patch `core.llm.groq_generate`、呼叫 `core.llm._load_groq_client()`；`backfill_words.py` import 的名字要顧到）。
- 無 `.gemini_key` → 行為與現狀完全相同（單 Groq）；連 `.groq_key` 都沒有 → `groq_generate(_strict)` 回 `""`（同現狀，不 raise）。
- Gemini 免費層配額寫成常數並註明「實作當下查官方文件」；模型 `gemini-2.0-flash`；key 檔 `.gemini_key`（加入 `.gitignore`）。
- 對話框／使用者可見字串一律英文；註解/docstring 中文可。
- commit 訊息：英文 type + 中文描述。

---

### Task 1: Headroom limiters（`core/providers.py` 上半）

**Files:**
- Create: `core/providers.py`
- Test: `test_providers.py`

**Interfaces:**
- Produces（Task 2、4 依賴）:
  - `HeaderLimiter(token_floor=1500)` — `.headroom() -> float`（0.0–1.0；未知→1.0；低於 floor→0.0）、`.reset_secs() -> float`、`.update(headers)`、`.mark_exhausted(retry_after)`
  - `LocalBucketLimiter(per_minute, window_secs=60.0)` — `.headroom()`、`.reset_secs()`、`.record_call()`、`.mark_exhausted(retry_after)`
  - 時間一律用模組層 `time.monotonic()`（測試 `patch.object(prov.time, "monotonic", ...)`），不做 clock 注入參數。
  - `_parse_int(v) -> int|None`、`_parse_reset_secs(s) -> float`（自 addon `_GroqLimiter` 移植）

- [ ] **Step 1: 寫失敗測試**

```python
# test_providers.py
"""core/providers.py 的 limiter 純邏輯測試（免網路、假時鐘）。"""
from unittest.mock import patch

import pytest

import core.providers as prov


def _headers(remaining, limit=12000, reset="30s"):
    return {"x-ratelimit-remaining-tokens": str(remaining),
            "x-ratelimit-limit-tokens": str(limit),
            "x-ratelimit-reset-tokens": reset}


class TestHeaderLimiter:
    def test_unknown_quota_is_full(self):
        assert prov.HeaderLimiter().headroom() == 1.0     # 還沒打過 → 視為充足

    def test_headroom_is_fraction_of_limit(self):
        lim = prov.HeaderLimiter()
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            lim.update(_headers(6000, limit=12000))
            assert lim.headroom() == pytest.approx(0.5)

    def test_below_floor_is_zero(self):
        lim = prov.HeaderLimiter(token_floor=1500)
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            lim.update(_headers(1000))
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(30.0, abs=0.2)

    def test_window_passed_refills(self):
        lim = prov.HeaderLimiter()
        clock = {"t": 1000.0}
        with patch.object(prov.time, "monotonic", side_effect=lambda: clock["t"]):
            lim.update(_headers(1000, reset="10s"))
            clock["t"] = 1020.0
            assert lim.headroom() == 1.0
            assert lim.reset_secs() == 0.0

    def test_mark_exhausted(self):
        lim = prov.HeaderLimiter()
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            lim.mark_exhausted(45)
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(45.0, abs=0.2)


class TestLocalBucketLimiter:
    def test_fresh_bucket_full(self):
        assert prov.LocalBucketLimiter(15).headroom() == 1.0

    def test_consumption_decreases(self):
        lim = prov.LocalBucketLimiter(15)
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            for _ in range(3):
                lim.record_call()
            assert lim.headroom() == pytest.approx(12 / 15)

    def test_exhausted_is_zero_with_reset(self):
        lim = prov.LocalBucketLimiter(15, window_secs=60.0)
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            for _ in range(15):
                lim.record_call()
            assert lim.headroom() == 0.0
            assert 0.0 < lim.reset_secs() <= 60.0

    def test_window_passes_refills(self):
        lim = prov.LocalBucketLimiter(15, window_secs=60.0)
        clock = {"t": 1000.0}
        with patch.object(prov.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(15):
                lim.record_call()
            clock["t"] = 1061.0
            assert lim.headroom() == 1.0

    def test_mark_exhausted(self):
        lim = prov.LocalBucketLimiter(15)
        with patch.object(prov.time, "monotonic", return_value=1000.0):
            lim.mark_exhausted(20)
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(20.0, abs=0.2)
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest test_providers.py -q`
Expected: `ModuleNotFoundError: No module named 'core.providers'`（collection error 也算紅）

- [ ] **Step 3: 最小實作**

```python
# core/providers.py
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
    """本地估算 headroom（Gemini 沒有 rate-limit header）：滾動窗口計數。"""

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
```

（注意：兩個 limiter 的時間一律呼叫模組層 `time.monotonic()`，測試用 `patch.object(prov.time, "monotonic", ...)` 打整個模組的 time。）

- [ ] **Step 4: 跑測試確認綠**

Run: `uv run pytest test_providers.py -q`
Expected: `11 passed`

- [ ] **Step 5: 跑全套確認沒壞別人**

Run: `uv run pytest -q`
Expected: `115 passed`（104 + 11）

---

### Task 2: Provider 類別（`core/providers.py` 下半）

**Files:**
- Modify: `core/providers.py`（附加在 Task 1 之後）
- Test: `test_providers.py`（附加）

**Interfaces:**
- Consumes: Task 1 的兩個 limiter。
- Produces（Task 4、5 依賴）:
  - `ProviderError(Exception)`；`ProviderRateLimited(ProviderError)`，屬性 `.retry_after: float`
  - `GroqProvider` — `.name == "groq"`、`.model`、`classmethod load() -> GroqProvider|None`、`.generate(prompt, *, temperature, max_tokens) -> str`（失敗 raise）、`.headroom()`、`.reset_secs()`
  - `GeminiProvider` — 同介面，`.name == "gemini"`
  - `_load_groq_client() -> Groq|None`（自 `core/llm.py` 搬來，llm.py 會 re-export）
  - `GROQ_MODEL`、`GEMINI_MODEL`、`_extract_gemini_text(data) -> str`、`_gemini_retry_secs(body) -> float`

- [ ] **Step 1: 寫失敗測試（附加到 test_providers.py）**

```python
class TestGeminiParsing:
    def test_extract_text(self):
        data = {"candidates": [{"content": {"parts": [{"text": " A cat sat. "}]}}]}
        assert prov._extract_gemini_text(data) == "A cat sat."

    def test_extract_empty_on_bad_shape(self):
        assert prov._extract_gemini_text({}) == ""
        assert prov._extract_gemini_text({"candidates": []}) == ""

    def test_retry_secs_from_429_body(self):
        body = '{"error": {"details": [{"retryDelay": "13s"}]}}'
        assert prov._gemini_retry_secs(body) == pytest.approx(13.0)

    def test_retry_secs_default(self):
        assert prov._gemini_retry_secs("junk") == pytest.approx(30.0)


class TestProviderLoad:
    def test_gemini_no_key_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prov, "GEMINI_KEY_PATH", str(tmp_path / "nope"))
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert prov.GeminiProvider.load() is None

    def test_gemini_key_file_loads(self, tmp_path, monkeypatch):
        p = tmp_path / ".gemini_key"
        p.write_text("AIzaFAKE\n")
        monkeypatch.setattr(prov, "GEMINI_KEY_PATH", str(p))
        g = prov.GeminiProvider.load()
        assert g is not None and g.name == "gemini"
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest test_providers.py -q`
Expected: 新增 6 個 FAIL/ERROR（`_extract_gemini_text` 等不存在）；原 11 個仍 PASS

- [ ] **Step 3: 實作（附加到 core/providers.py）**

```python
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
            return raw.parse().choices[0].message.content.strip()
        except RateLimitError as e:
            secs = _retry_after_from(e)
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
            secs = _gemini_retry_secs(r.text)
            self._limiter.mark_exhausted(secs)
            raise ProviderRateLimited(secs)
        if r.status_code != 200:
            raise ProviderError(f"HTTP {r.status_code}")
        text = _extract_gemini_text(r.json())
        if not text:
            raise ProviderError("empty response")
        return text

    def headroom(self):
        return self._limiter.headroom()

    def reset_secs(self):
        return self._limiter.reset_secs()
```

- [ ] **Step 4: 跑測試確認綠**

Run: `uv run pytest test_providers.py -q`
Expected: `17 passed`

- [ ] **Step 5: Commit**

```bash
git add core/providers.py test_providers.py
git commit -m "feat: LLM provider 層(Groq header limiter + Gemini 本地 bucket)"
```
（commit 訊息含中文描述；結尾照慣例加 Co-Authored-By。）

---

### Task 3: CircuitBreaker（`core/dispatcher.py` 上半）

**Files:**
- Create: `core/dispatcher.py`
- Test: `test_dispatcher.py`

**Interfaces:**
- Produces（Task 4 依賴）:
  - `CircuitBreaker(threshold=3, cooldown_default=30.0)` — `.allows() -> bool`、`.record_success()`、`.record_failure(cooldown=None)`、`.open_remaining() -> float`

- [ ] **Step 1: 寫失敗測試**

```python
# test_dispatcher.py
"""dispatcher 的路由/斷路器/failover 純邏輯測試（假 provider、假時鐘、免網路）。"""
from unittest.mock import patch

import pytest

import core.dispatcher as disp


class TestCircuitBreaker:
    def test_starts_closed(self):
        assert disp.CircuitBreaker().allows() is True

    def test_opens_after_threshold(self):
        b = disp.CircuitBreaker(threshold=3)
        with patch.object(disp.time, "monotonic", return_value=1000.0):
            for _ in range(3):
                b.record_failure()
            assert b.allows() is False
            assert b.open_remaining() == pytest.approx(30.0, abs=0.2)

    def test_below_threshold_stays_closed(self):
        b = disp.CircuitBreaker(threshold=3)
        b.record_failure()
        b.record_failure()
        assert b.allows() is True

    def test_success_resets_streak(self):
        b = disp.CircuitBreaker(threshold=3)
        b.record_failure()
        b.record_failure()
        b.record_success()
        b.record_failure()
        b.record_failure()
        assert b.allows() is True          # 中間成功過 → 連續失敗歸零

    def test_half_open_after_cooldown_then_success_closes(self):
        b = disp.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(disp.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            clock["t"] = 1031.0
            assert b.allows() is True       # half-open：放試探
            b.record_success()
            assert b.allows() is True       # 復位

    def test_half_open_probe_failure_reopens(self):
        b = disp.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(disp.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            clock["t"] = 1031.0
            assert b.allows() is True
            b.record_failure()              # 試探失敗 → 立刻再 OPEN
            assert b.allows() is False

    def test_failure_cooldown_uses_provider_reset(self):
        b = disp.CircuitBreaker(threshold=1, cooldown_default=30.0)
        with patch.object(disp.time, "monotonic", return_value=1000.0):
            b.record_failure(cooldown=12.0)    # 冷卻=該家 reset 時間，不瞎猜
            assert b.open_remaining() == pytest.approx(12.0, abs=0.2)
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest test_dispatcher.py -q`
Expected: `ModuleNotFoundError: No module named 'core.dispatcher'`

- [ ] **Step 3: 最小實作**

```python
# core/dispatcher.py
"""Capacity-aware dispatcher over multiple LLM providers.

路由：每筆呼叫挑 headroom（剩餘額度比例）最大且斷路器放行的 provider。
斷路器：連續失敗 threshold 次 → OPEN（冷卻 = 該家 reset 時間，拿不到用預設）→
冷卻過後 HALF-OPEN 放一筆試探。failover：選中的失敗 → 依序換下一家。
兩家都不可用 → raise AllProvidersLimited（帶每家 reset 秒數，政策由呼叫端決定）。
"""
import threading
import time

from core.providers import ProviderError


class CircuitBreaker:
    """三態斷路器：CLOSED →（連續失敗達 threshold）→ OPEN →（冷卻到期）→ HALF-OPEN。"""

    def __init__(self, threshold=3, cooldown_default=30.0):
        self._threshold = threshold
        self._cooldown_default = cooldown_default
        self._lock = threading.Lock()
        self._streak = 0
        self._open_until = 0.0

    def allows(self):
        with self._lock:
            return time.monotonic() >= self._open_until

    def open_remaining(self):
        with self._lock:
            return max(0.0, self._open_until - time.monotonic())

    def record_success(self):
        with self._lock:
            self._streak = 0
            self._open_until = 0.0

    def record_failure(self, cooldown=None):
        with self._lock:
            self._streak += 1
            if self._streak >= self._threshold:
                secs = cooldown if cooldown is not None else self._cooldown_default
                self._open_until = time.monotonic() + float(secs)
```

（half-open 的「放一筆試探」由 `allows()` 在冷卻過後回 True 自然形成；試探失敗時 `_streak` 已 >= threshold，`record_failure` 再度設 `_open_until` → 重新 OPEN。）

- [ ] **Step 4: 跑測試確認綠**

Run: `uv run pytest test_dispatcher.py -q`
Expected: `7 passed`

---

### Task 4: Dispatcher（`core/dispatcher.py` 下半）

**Files:**
- Modify: `core/dispatcher.py`（附加）
- Test: `test_dispatcher.py`（附加）

**Interfaces:**
- Consumes: Task 3 `CircuitBreaker`；Task 2 `ProviderError`/`ProviderRateLimited`。
- Produces（Task 5 依賴）:
  - `AllProvidersLimited(Exception)` — `.resets: dict[str, float]`、`.soonest_reset: float`
  - `Dispatcher(providers, threshold=3, cooldown_default=30.0)` — `.generate(prompt, *, temperature=0.7, max_tokens=200) -> str`、`.providers: list`

- [ ] **Step 1: 寫失敗測試（附加到 test_dispatcher.py）**

（同時在 test_dispatcher.py 檔頭 import 區補上：`from core.providers import ProviderError`）

```python
class FakeProvider:
    """可編程假 provider：headroom 可為值或 callable；replies/errors 腳本化。"""
    def __init__(self, name, headroom=1.0, reset=0.0, reply="ok", error=None):
        self.name = name
        self._headroom = headroom
        self._reset = reset
        self._reply = reply
        self._error = error
        self.calls = 0

    def generate(self, prompt, **kw):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._reply

    def headroom(self):
        return self._headroom() if callable(self._headroom) else self._headroom

    def reset_secs(self):
        return self._reset


class TestRouting:
    def test_picks_higher_headroom(self):
        a = FakeProvider("groq", headroom=0.3, reply="from-groq")
        b = FakeProvider("gemini", headroom=0.9, reply="from-gemini")
        d = disp.Dispatcher([a, b])
        assert d.generate("p") == "from-gemini"
        assert (a.calls, b.calls) == (0, 1)

    def test_switches_when_headroom_shifts(self):
        state = {"h": 0.9}
        a = FakeProvider("groq", headroom=0.5, reply="from-groq")
        b = FakeProvider("gemini", headroom=lambda: state["h"], reply="from-gemini")
        d = disp.Dispatcher([a, b])
        assert d.generate("p") == "from-gemini"
        state["h"] = 0.1                      # gemini 額度被打低
        assert d.generate("p") == "from-groq"
        state["h"] = 1.0                      # 窗口 reset，額度恢復 → 路由自動回來
        assert d.generate("p") == "from-gemini"

    def test_zero_headroom_excluded(self):
        a = FakeProvider("groq", headroom=0.0)
        b = FakeProvider("gemini", headroom=0.2, reply="from-gemini")
        d = disp.Dispatcher([a, b])
        assert d.generate("p") == "from-gemini"
        assert a.calls == 0


class TestFailover:
    def test_failover_to_other_provider(self):
        a = FakeProvider("groq", headroom=0.9, error=ProviderError("boom"))
        b = FakeProvider("gemini", headroom=0.5, reply="rescued")
        d = disp.Dispatcher([a, b])
        assert d.generate("p") == "rescued"
        assert (a.calls, b.calls) == (1, 1)

    def test_all_fail_raises_limited(self):
        a = FakeProvider("groq", headroom=0.9, reset=40.0, error=ProviderError("x"))
        b = FakeProvider("gemini", headroom=0.5, reset=15.0, error=ProviderError("y"))
        d = disp.Dispatcher([a, b])
        with pytest.raises(disp.AllProvidersLimited) as ei:
            d.generate("p")
        assert ei.value.soonest_reset == pytest.approx(15.0)
        assert set(ei.value.resets) == {"groq", "gemini"}

    def test_both_exhausted_raises_limited_without_calling(self):
        a = FakeProvider("groq", headroom=0.0, reset=40.0)
        b = FakeProvider("gemini", headroom=0.0, reset=15.0)
        d = disp.Dispatcher([a, b])
        with pytest.raises(disp.AllProvidersLimited) as ei:
            d.generate("p")
        assert (a.calls, b.calls) == (0, 0)
        assert ei.value.resets["groq"] == pytest.approx(40.0)
        assert ei.value.soonest_reset == pytest.approx(15.0)


class TestBreakerIntegration:
    def test_broken_provider_skipped_until_cooldown(self):
        err = ProviderError("boom")
        a = FakeProvider("groq", headroom=0.9, error=err)
        b = FakeProvider("gemini", headroom=0.5, reply="ok")
        clock = {"t": 1000.0}
        with patch.object(disp.time, "monotonic", side_effect=lambda: clock["t"]):
            d = disp.Dispatcher([a, b], threshold=3, cooldown_default=30.0)
            for _ in range(3):
                d.generate("p")            # groq 每次先被選中、失敗、failover 到 gemini
            assert a.calls == 3            # 3 次後 groq 斷路器 OPEN
            d.generate("p")
            assert a.calls == 3            # OPEN 期間 groq 不再被打
            clock["t"] = 1031.0            # 冷卻過 → half-open 試探
            a._error = None
            a._reply = "groq-back"
            assert d.generate("p") == "groq-back"
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest test_dispatcher.py -q`
Expected: 新增測試 FAIL（`Dispatcher`/`AllProvidersLimited` 不存在）；斷路器 7 個仍 PASS

- [ ] **Step 3: 實作（附加到 core/dispatcher.py）**

```python
class AllProvidersLimited(Exception):
    """所有 provider 都不可用（額度見底/熔斷/失敗）。resets: 每家幾秒後恢復。"""

    def __init__(self, resets):
        super().__init__("all providers limited")
        self.resets = dict(resets)
        self.soonest_reset = min(self.resets.values()) if self.resets else 60.0


class Dispatcher:
    """單一決策點：過濾（斷路器/headroom）→ 挑 headroom 最大 → 打 → 失敗 failover。"""

    def __init__(self, providers, threshold=3, cooldown_default=30.0):
        self.providers = list(providers)
        self._breakers = {p.name: CircuitBreaker(threshold, cooldown_default)
                          for p in self.providers}

    def _resets(self):
        out = {}
        for p in self.providers:
            out[p.name] = max(p.reset_secs(), self._breakers[p.name].open_remaining())
        return out

    def generate(self, prompt, *, temperature=0.7, max_tokens=200):
        candidates = sorted(
            (p for p in self.providers
             if self._breakers[p.name].allows() and p.headroom() > 0.0),
            key=lambda p: p.headroom(), reverse=True)
        if not candidates:
            raise AllProvidersLimited(self._resets())
        for p in candidates:                      # 第一家失敗 → 依序 failover
            try:
                text = p.generate(prompt, temperature=temperature, max_tokens=max_tokens)
                self._breakers[p.name].record_success()
                return text
            except ProviderError as e:
                cooldown = getattr(e, "retry_after", None)
                self._breakers[p.name].record_failure(cooldown)
        raise AllProvidersLimited(self._resets())
```

- [ ] **Step 4: 跑測試確認綠**

Run: `uv run pytest test_dispatcher.py -q`
Expected: `14 passed`

- [ ] **Step 5: Commit**

```bash
git add core/dispatcher.py test_dispatcher.py
git commit -m "feat: LLM dispatcher(容量感知路由+斷路器+failover)"
```

---

### Task 5: 接線 `core/llm.py` + 收尾

**Files:**
- Modify: `core/llm.py:1-74`（import 區與 groq_generate/_strict/llm；`_retry_after_from` 移除——已搬進 providers）
- Modify: `backfill_words.py:10,141`（引擎橫幅改問 dispatcher）
- Modify: `.gitignore`（加 `.gemini_key`）
- Modify: `README.md`、`CLAUDE.md`（文件同步）
- Test: `test_llm_wiring.py`（新）

**Interfaces:**
- Consumes: `Dispatcher`、`AllProvidersLimited`、`GroqProvider`、`GeminiProvider`、`_load_groq_client`、`GROQ_MODEL`。
- Produces: `groq_generate(prompt)`、`groq_generate_strict(prompt)`（簽名不變）、`engine_description() -> str`（backfill_words 橫幅用）。
- 不變式: `test_backfill.py` patch `core.llm.groq_generate` 與呼叫 `core.llm._load_groq_client()` 都要照常可用。

- [ ] **Step 1: 寫失敗測試**

```python
# test_llm_wiring.py
"""core/llm.py 接上 dispatcher 後的行為測試（假 dispatcher 注入、免網路）。"""
from unittest.mock import patch

import pytest

import core.llm as llm_mod
from core.dispatcher import AllProvidersLimited, Dispatcher
from core.rate_limiter import RateLimitReached


class _FakeDispatcher:
    def __init__(self, reply=None, exc=None, providers=("x",)):
        self.providers = list(providers)
        self._reply = reply
        self._exc = exc
        self.kwargs = None

    def generate(self, prompt, **kw):
        self.kwargs = kw
        if self._exc:
            raise self._exc
        return self._reply


class TestWiring:
    def test_generate_returns_text(self):
        with patch.object(llm_mod, "_dispatcher", _FakeDispatcher(reply="hi")):
            assert llm_mod.groq_generate("p") == "hi"

    def test_generate_swallows_limited(self):
        exc = AllProvidersLimited({"groq": 40.0})
        with patch.object(llm_mod, "_dispatcher", _FakeDispatcher(exc=exc)):
            assert llm_mod.groq_generate("p") == ""

    def test_strict_translates_limited_to_ratelimitreached(self):
        exc = AllProvidersLimited({"groq": 40.0, "gemini": 15.0})
        with patch.object(llm_mod, "_dispatcher", _FakeDispatcher(exc=exc)):
            with pytest.raises(RateLimitReached) as ei:
                llm_mod.groq_generate_strict("p")
            assert ei.value.retry_after == 16          # soonest(15) 無條件進位 +1

    def test_no_providers_returns_empty(self):
        with patch.object(llm_mod, "_dispatcher", _FakeDispatcher(providers=())):
            assert llm_mod.groq_generate("p") == ""
            assert llm_mod.groq_generate_strict("p") == ""

    def test_strict_uses_lower_temperature(self):
        fake = _FakeDispatcher(reply="x")
        with patch.object(llm_mod, "_dispatcher", fake):
            llm_mod.groq_generate_strict("p")
            assert fake.kwargs["temperature"] == 0.3
            llm_mod.groq_generate("p")
            assert fake.kwargs["temperature"] == 0.7

    def test_engine_description_lists_providers(self):
        class _P:
            def __init__(self, name, model):
                self.name, self.model = name, model
        fake = _FakeDispatcher()
        fake.providers = [_P("groq", "llama-3.3-70b-versatile"), _P("gemini", "gemini-2.0-flash")]
        with patch.object(llm_mod, "_dispatcher", fake):
            s = llm_mod.engine_description()
            assert "groq" in s and "gemini" in s

    def test_load_groq_client_still_importable(self):
        assert callable(llm_mod._load_groq_client)     # test_backfill.py 靠它
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest test_llm_wiring.py -q`
Expected: FAIL/ERROR（`_dispatcher`、`engine_description` 不存在）

- [ ] **Step 3: 改 `core/llm.py`**

把檔頭到 `llm()`（現在的 1–74 行）換成：

```python
import os
import re

from core.dispatcher import AllProvidersLimited, Dispatcher
from core.providers import (GROQ_MODEL, GeminiProvider, GroqProvider,
                            _load_groq_client)
from core.rate_limiter import RateLimitReached

_dispatcher = Dispatcher(
    [p for p in (GroqProvider.load(), GeminiProvider.load()) if p])


def engine_description():
    """人看的引擎清單（backfill_words 橫幅）。"""
    if not _dispatcher.providers:
        return "no LLM (set .groq_key / .gemini_key)"
    return " + ".join(f"{p.name} ({p.model})" for p in _dispatcher.providers)


def groq_generate(prompt):
    """單發生成：任何失敗（含兩家見底）靜默回 ''。名字保留舊稱以免動全部呼叫端。"""
    if not _dispatcher.providers:
        return ""
    try:
        return _dispatcher.generate(prompt, temperature=0.7, max_tokens=200)
    except AllProvidersLimited:
        return ""
    except Exception as e:
        print(f"  [llm error] {e}")
        return ""


def groq_generate_strict(prompt):
    """批次用：兩家見底 → 翻譯成既有 RateLimitReached（retry=最快恢復那家），
    既有 pacing 呼叫端一行不改。"""
    if not _dispatcher.providers:
        return ""
    try:
        return _dispatcher.generate(prompt, temperature=0.3, max_tokens=200)
    except AllProvidersLimited as e:
        raise RateLimitReached(int(e.soonest_reset) + 1)


def llm(prompt):
    return groq_generate(prompt)
```

（`GROQ_KEY_PATH`/`_load_groq_client`/`_retry_after_from`/`_groq_client` 從 llm.py 移除——loader 與 retry 解析已住在 providers.py；`_load_groq_client` 以 import 方式 re-export 供 `test_backfill.py` 使用。其餘 prompt 函式一律不動。）

- [ ] **Step 4: 改 `backfill_words.py` 橫幅**

第 10 行 import 改成：

```python
from core.llm import llm_sentence_and_query, llm_translate, engine_description
```

第 141 行改成：

```python
    engine = engine_description()
```

- [ ] **Step 5: `.gitignore` 加一行**

在 `.groq_key` 下面加：

```
.gemini_key
```

- [ ] **Step 6: 跑全套測試**

Run: `uv run pytest -q`
Expected: 全綠（104 + 11 + 6 + 14 + 7 ≈ 142 passed；`test_backfill.py`、`test_llm_prompts.py` 不得變紅）

- [ ] **Step 7: 真網路煙霧測試（需 .groq_key + .gemini_key）**

Run:
```bash
uv run python -c "
from core.llm import engine_description, llm_sentence
print(engine_description())
print(llm_sentence('resilience'))"
```
Expected: 第一行列出 `groq (llama-3.3-70b-versatile) + gemini (gemini-2.0-flash)`；第二行一句短例句。

- [ ] **Step 8: 文件同步**

README.md「架構總覽」段落把 LLM 描述改成雙 provider（Groq + Gemini 容量感知分流，`.gemini_key` 可選、沒有就單 Groq），FAQ 或安裝段補一行 `.gemini_key` 怎麼放。CLAUDE.md Key Rules 加一條：

```
- core 的 LLM 文字呼叫一律走 `core/dispatcher.py`（容量感知分流 Groq+Gemini、斷路器、failover）；
  provider 在 `core/providers.py`。Gemini 沒有 rate-limit header → 本地 bucket（配額常數 `GEMINI_RPM`）。
  兩家見底時 `groq_generate_strict` 翻譯成 `RateLimitReached(soonest_reset)`。addon 尚未接（Phase 2），
  仍是單 Groq。`backfill_words.py` 橫幅用 `engine_description()`。
```

- [ ] **Step 9: Commit**

```bash
git add core/llm.py backfill_words.py .gitignore README.md CLAUDE.md test_llm_wiring.py
git commit -m "feat: core LLM 呼叫接上 Groq+Gemini 分流(呼叫端零改動)"
```

---

## 驗收（對照 spec 成功標準）

1. `uv run pytest -q` 全綠。
2. 有 `.gemini_key`：Task 5 Step 7 煙霧測試看到兩家都列出；跑 `backfill_sentence_cn.py` 大批量可觀察交替選用（headroom 消長）。
3. 刪掉/改名 `.gemini_key` 再跑煙霧測試 → 只列 groq、功能照常 = 退化正確。
