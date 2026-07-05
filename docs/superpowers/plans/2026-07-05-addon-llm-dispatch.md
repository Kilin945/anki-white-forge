# Addon LLM 分流鏡像（Phase 2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** addon（⌘A/⌘S/批量面板）的 LLM 呼叫從單 Groq 換成 Groq+Gemini 容量感知分流（core 韌性層的鏡像緊湊版）。

**Architecture:** 新自足子模組 `addon/_llm_dispatch.py`（stdlib-only、零 aqt、KEEP-IN-SYNC 對照 core）承載 limiter/breaker/provider/dispatcher；`addon/__init__.py` 的 `_groq_chat` 簽名不變、內裡改走 dispatcher，⌘S 停批閘改「兩家都不可用才停」，撞限訊息帶兩家真實 reset 時間。

**Tech Stack:** Python（Anki 內建版）、urllib、pytest（假時鐘 `patch time.monotonic`、`importlib.util.spec_from_file_location` 直接載入子模組——免假 aqt stub）。

**Spec:** `docs/superpowers/specs/2026-07-05-addon-llm-dispatch-design.md`

## Global Constraints

- `addon/_llm_dispatch.py` **自足**：只用 stdlib（json/os/re/threading/time/urllib），無相對匯入、無 aqt → 可被 `spec_from_file_location` 單獨載入。
- 與 core 的**語意一致**（KEEP-IN-SYNC 檔頭標注）；刻意差異僅四項：provider/dispatcher 的 `generate(...)` 多收 `timeout`；`Dispatcher.wall_secs()`；`Dispatcher.resets()`（公開）；`format_reset_summary()`。`CircuitBreaker` 多一個唯讀 `would_allow()`（wall_secs 預檢用，**不消費**試探閘）。
- `_groq_chat(prompt, *, temperature, max_tokens, timeout, strict=False)` 簽名不變；4 個消費者（拼字/造句/單字翻譯/整句翻譯）與 `SentenceCNWorker` 的 `_AddonRateLimited` pacing **零改動**。
- Groq 呼叫必帶 `User-Agent: AnkiWordAdder/1.0`；Gemini key 走 `x-goog-api-key` header。
- 對話框 UI 文字英文；註解/docstring 中文。
- 測試不打真網路；時間用 `patch.object(<module>.time, "monotonic", ...)`。
- 既有 151 個測試除 `test_groq_limiter.py`（本計畫改造它）外全部保持綠。
- **消費者盤點**（Phase 1 `_groq_client` 事故教訓）：被移除符號（`_GroqLimiter`、`_groq_limiter`、`_parse_int`、`_parse_reset_secs`、`_parse_retry_after`、`_load_groq_key`、`GROQ_API_URL`）的消費者只有 `_groq_chat` 內裡、`BackfillWorker._process_one` 的 wall 檢查、`test_groq_limiter.py` —— 本計畫全數處理；commit 前 grep 驗證零殘留。
- commit 訊息：英文 type + 中文描述，結尾 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

### Task 1: 子模組 `addon/_llm_dispatch.py` + 測試

**Files:**
- Create: `addon/_llm_dispatch.py`
- Test: `test_addon_llm_dispatch.py`

**Interfaces:**
- Consumes: 無（自足模組；邏輯鏡像 `core/providers.py` + `core/dispatcher.py`，實作前先讀這兩檔對照）。
- Produces（Task 2 依賴，全在 `_llm_dispatch` 命名空間）:
  - `GroqProvider.load() -> GroqProvider|None`、`GeminiProvider.load() -> GeminiProvider|None`（`.name`/`.model`/`.generate(prompt, *, temperature, max_tokens, timeout)`/`.headroom()`/`.reset_secs()`）
  - `Dispatcher(providers)` — `.providers`、`.generate(prompt, *, temperature, max_tokens, timeout)`、`.wall_secs() -> float`、`.resets() -> dict[str, float]`
  - `AllProvidersLimited`（`.resets`、`.soonest_reset`）、`ProviderError`、`ProviderRateLimited(retry_after)`
  - `format_reset_summary(resets) -> str`

- [ ] **Step 1: 寫失敗測試**

```python
# test_addon_llm_dispatch.py
"""addon/_llm_dispatch.py（addon 端 LLM 分流鏡像）測試。

子模組自足(stdlib-only、無 aqt) → 直接從檔案載入,不需要假 aqt stub。
鏡像 core 測試面(test_providers/test_dispatcher),外加 addon 特有的
timeout 傳遞、wall_secs()、format_reset_summary()。假時鐘、免網路。
"""
import importlib.util
import pathlib
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "addon_llm_dispatch",
    pathlib.Path(__file__).parent / "addon" / "_llm_dispatch.py")
lld = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lld)


def _headers(remaining, limit=12000, reset="30s"):
    return {"x-ratelimit-remaining-tokens": str(remaining),
            "x-ratelimit-limit-tokens": str(limit),
            "x-ratelimit-reset-tokens": reset}


class FakeProvider:
    """可編程假 provider(同 core 測試的形狀,多收 timeout)。"""
    def __init__(self, name, headroom=1.0, reset=0.0, reply="ok", error=None):
        self.name = name
        self._headroom = headroom
        self._reset = reset
        self._reply = reply
        self._error = error
        self.calls = 0
        self.last_kwargs = None

    def generate(self, prompt, **kw):
        self.calls += 1
        self.last_kwargs = kw
        if self._error is not None:
            raise self._error
        return self._reply

    def headroom(self):
        return self._headroom() if callable(self._headroom) else self._headroom

    def reset_secs(self):
        return self._reset


class TestHeaderLimiter:
    def test_unknown_quota_is_full(self):
        assert lld.HeaderLimiter().headroom() == 1.0

    def test_headroom_is_fraction_and_floor(self):
        lim = lld.HeaderLimiter(token_floor=1500)
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            lim.update(_headers(6000))
            assert lim.headroom() == pytest.approx(0.5)
            lim.update(_headers(1000))
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(30.0, abs=0.2)

    def test_window_passed_refills(self):
        lim = lld.HeaderLimiter()
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            lim.update(_headers(1000, reset="10s"))
            clock["t"] = 1020.0
            assert lim.headroom() == 1.0

    def test_mark_exhausted(self):
        lim = lld.HeaderLimiter()
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            lim.mark_exhausted(45)
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(45.0, abs=0.2)


class TestLocalBucketLimiter:
    def test_consume_exhaust_refill(self):
        lim = lld.LocalBucketLimiter(15, window_secs=60.0)
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            assert lim.headroom() == 1.0
            for _ in range(15):
                lim.record_call()
            assert lim.headroom() == 0.0
            assert 0.0 < lim.reset_secs() <= 60.0
            clock["t"] = 1061.0
            assert lim.headroom() == 1.0

    def test_mark_exhausted(self):
        lim = lld.LocalBucketLimiter(15)
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            lim.mark_exhausted(20)
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(20.0, abs=0.2)


class TestCircuitBreaker:
    def test_opens_after_threshold_and_probes(self):
        b = lld.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            assert b.allows() is False
            clock["t"] = 1031.0
            assert b.allows() is True          # half-open:放一筆
            assert b.allows() is False         # 單筆試探閘
            b.record_success()
            assert b.allows() is True          # 復位

    def test_probe_failure_reopens(self):
        b = lld.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            clock["t"] = 1031.0
            assert b.allows() is True
            b.record_failure()
            assert b.allows() is False

    def test_would_allow_does_not_consume_probe(self):
        b = lld.CircuitBreaker(threshold=3, cooldown_default=30.0)
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            for _ in range(3):
                b.record_failure()
            clock["t"] = 1031.0
            assert b.would_allow() is True     # 窺看
            assert b.would_allow() is True     # 再窺看,沒消費
            assert b.allows() is True          # 真的取試探
            assert b.would_allow() is False    # 試探在途 → 窺看也 False
            b.record_success()

    def test_failure_cooldown_uses_retry_after(self):
        b = lld.CircuitBreaker(threshold=1, cooldown_default=30.0)
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            b.record_failure(cooldown=12.0)
            assert b.open_remaining() == pytest.approx(12.0, abs=0.2)


class TestDispatcher:
    def test_picks_higher_headroom_and_passes_timeout(self):
        a = FakeProvider("groq", headroom=0.3, reply="from-groq")
        b = FakeProvider("gemini", headroom=0.9, reply="from-gemini")
        d = lld.Dispatcher([a, b])
        assert d.generate("p", temperature=0.5, max_tokens=64, timeout=9) == "from-gemini"
        assert b.last_kwargs == {"temperature": 0.5, "max_tokens": 64, "timeout": 9}
        assert a.calls == 0

    def test_switches_when_headroom_shifts(self):
        state = {"h": 0.9}
        a = FakeProvider("groq", headroom=0.5, reply="from-groq")
        b = FakeProvider("gemini", headroom=lambda: state["h"], reply="from-gemini")
        d = lld.Dispatcher([a, b])
        assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "from-gemini"
        state["h"] = 0.1
        assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "from-groq"
        state["h"] = 1.0
        assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "from-gemini"

    def test_failover_and_all_limited(self):
        a = FakeProvider("groq", headroom=0.9, reset=40.0, error=lld.ProviderError("x"))
        b = FakeProvider("gemini", headroom=0.5, reply="rescued")
        d = lld.Dispatcher([a, b])
        assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "rescued"
        b._error = lld.ProviderError("y")
        b._reset = 15.0
        with pytest.raises(lld.AllProvidersLimited) as ei:
            d.generate("p", temperature=0, max_tokens=8, timeout=8)
        assert ei.value.soonest_reset == pytest.approx(15.0)
        assert set(ei.value.resets) == {"groq", "gemini"}

    def test_unexpected_exception_recorded_and_reraised(self):
        boom = FakeProvider("groq", headroom=0.9, error=ValueError("bug"))
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            d = lld.Dispatcher([boom], threshold=1, cooldown_default=30.0)
            with pytest.raises(ValueError):
                d.generate("p", temperature=0, max_tokens=8, timeout=8)
            clock["t"] = 1031.0
            boom._error = None
            boom._reply = "ok"
            assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "ok"


class TestWallSecs:
    def test_zero_when_any_provider_available(self):
        a = FakeProvider("groq", headroom=0.0, reset=40.0)
        b = FakeProvider("gemini", headroom=0.6)
        assert lld.Dispatcher([a, b]).wall_secs() == 0.0

    def test_soonest_reset_when_all_walled(self):
        a = FakeProvider("groq", headroom=0.0, reset=40.0)
        b = FakeProvider("gemini", headroom=0.0, reset=15.0)
        assert lld.Dispatcher([a, b]).wall_secs() == pytest.approx(15.0)

    def test_does_not_consume_half_open_probe(self):
        p = FakeProvider("groq", headroom=0.9, error=lld.ProviderError("x"))
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            d = lld.Dispatcher([p], threshold=1, cooldown_default=30.0)
            with pytest.raises(lld.AllProvidersLimited):
                d.generate("p", temperature=0, max_tokens=8, timeout=8)
            clock["t"] = 1031.0                 # half-open
            assert d.wall_secs() == 0.0          # 預檢看得到「可試探」
            assert d.wall_secs() == 0.0          # 且不消費試探閘
            p._error = None
            p._reply = "back"                    # 試探仍可用
            assert d.generate("p", temperature=0, max_tokens=8, timeout=8) == "back"


class TestParsersAndHelpers:
    @pytest.mark.parametrize("s, expected", [
        ("1m26.4s", 86.4), ("185ms", 0.185), ("2.5s", 2.5),
        ("1h2m", 3720.0), ("", 0.0), (None, 0.0), ("garbage", 0.0),
    ])
    def test_parse_reset_secs(self, s, expected):
        assert lld._parse_reset_secs(s) == pytest.approx(expected)

    def test_parse_int(self):
        assert lld._parse_int("999") == 999
        assert lld._parse_int(None) is None

    def test_extract_gemini_text(self):
        data = {"candidates": [{"content": {"parts": [{"text": " hi "}]}}]}
        assert lld._extract_gemini_text(data) == "hi"
        assert lld._extract_gemini_text({}) == ""

    def test_gemini_retry_secs(self):
        assert lld._gemini_retry_secs('{"error":{"details":[{"retryDelay":"13s"}]}}') == pytest.approx(13.0)
        assert lld._gemini_retry_secs("junk") == pytest.approx(30.0)

    def test_format_reset_summary(self):
        s = lld.format_reset_summary({"groq": 40.2, "gemini": 15.7})
        assert "Groq" in s and "Gemini" in s
        assert "~40s" in s and "~15s" in s

    def test_load_without_key_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(lld, "GEMINI_KEY_PATH", str(tmp_path / "nope"))
        assert lld.GeminiProvider.load() is None
        monkeypatch.setattr(lld, "GROQ_KEY_PATH", str(tmp_path / "nope2"))
        assert lld.GroqProvider.load() is None
```

- [ ] **Step 2: 跑測試確認紅**

Run: `uv run pytest test_addon_llm_dispatch.py -q`
Expected: 載入即失敗（`FileNotFoundError` / module 不存在）——collection error 也算紅

- [ ] **Step 3: 實作 `addon/_llm_dispatch.py`**

```python
# addon/_llm_dispatch.py
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
import os
import re
import threading
import time
import urllib.error
import urllib.request

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


def _load_key(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


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
        key = _load_key(GROQ_KEY_PATH)
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
        except (KeyError, IndexError, TypeError):
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
        key = _load_key(GEMINI_KEY_PATH)
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
            try:
                text = p.generate(prompt, temperature=temperature,
                                  max_tokens=max_tokens, timeout=timeout)
                self._breakers[p.name].record_success()
                return text
            except ProviderError as e:
                cooldown = getattr(e, "retry_after", None)
                self._breakers[p.name].record_failure(cooldown)
            except Exception:
                self._breakers[p.name].record_failure()   # 非預期例外也要釋放試探閘
                raise
        raise AllProvidersLimited(self.resets())


def format_reset_summary(resets):
    """撞限訊息的英文摘要：'Gemini resets in ~15s, Groq in ~40s'（依名稱排序求穩定）。"""
    parts = []
    for i, (name, secs) in enumerate(sorted(resets.items())):
        label = name.capitalize()
        parts.append(f"{label} resets in ~{int(secs)}s" if i == 0
                     else f"{label} in ~{int(secs)}s")
    return ", ".join(parts)
```

- [ ] **Step 4: 跑測試確認綠**

Run: `uv run pytest test_addon_llm_dispatch.py -q`
Expected: `29 passed`（parametrize 展開後）

- [ ] **Step 5: 跑全套（既有不壞）**

Run: `uv run pytest -q`
Expected: `180 passed`（151 + 29；`test_groq_limiter.py` 此時還沒改、仍測 `addon.__init__` 裡的舊 `_GroqLimiter`，Task 2 才移除 → 此步必須全綠）

- [ ] **Step 6: Commit**

```bash
git add addon/_llm_dispatch.py test_addon_llm_dispatch.py
git commit -m "feat: addon LLM 分流鏡像子模組(_llm_dispatch,自足可測)"
```

---

### Task 2: `addon/__init__.py` 接線 + 測試遷移 + 文件

**Files:**
- Modify: `addon/__init__.py`（30-32 常數、61-68 `_load_groq_key`、141-190 limiter 區、193-237 `_groq_chat` 區、~727 wall 檢查、~714-718 worker init、~1002-1005 撞限訊息）
- Modify: `test_groq_limiter.py`（整檔改造：改測子模組）
- Modify: `README.md`、`CLAUDE.md`（文件同步）

**Interfaces:**
- Consumes: Task 1 的 `_llm_dispatch` 全部匯出（見 Task 1 Produces）。
- Produces: `_groq_chat` 簽名不變；`_AddonRateLimited` 保留原地；`BackfillWorker.limit_resets: dict` 新屬性（對話框訊息用）。

- [ ] **Step 1: 改造 `test_groq_limiter.py`（先寫，此時它會紅一半）**

整檔替換為（原壓力測試精神保留、對象改子模組的 `HeaderLimiter`；甩掉假 aqt stub）：

```python
# test_groq_limiter.py
"""壓力測試:addon 端 HeaderLimiter(addon/_llm_dispatch.py)。

原本測 addon/__init__.py 的 _GroqLimiter;Phase 2 該邏輯搬進自足子模組
_llm_dispatch.py(升級為 headroom 介面) → 直接檔案載入測,不再需要假 aqt stub。
"""
import importlib.util
import pathlib
import threading
from unittest.mock import patch

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "addon_llm_dispatch",
    pathlib.Path(__file__).parent / "addon" / "_llm_dispatch.py")
lld = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lld)


def _headers(remaining_tokens, reset="50ms"):
    return {"x-ratelimit-remaining-tokens": str(remaining_tokens),
            "x-ratelimit-reset-tokens": reset}


class TestParsers:
    @pytest.mark.parametrize("s, expected", [
        ("1m26.4s", 86.4), ("185ms", 0.185), ("2.5s", 2.5),
        ("1h2m", 3720.0), ("", 0.0), (None, 0.0), ("garbage", 0.0),
    ])
    def test_parse_reset_secs(self, s, expected):
        assert lld._parse_reset_secs(s) == pytest.approx(expected)

    def test_parse_int(self):
        assert lld._parse_int("999") == 999
        assert lld._parse_int(None) is None
        assert lld._parse_int("nope") is None


class TestWallDetection:
    """牆偵測語意沿用:headroom()==0 即「該停」,reset_secs() 即等待秒數。"""

    def test_clear_before_any_response(self):
        assert lld.HeaderLimiter().headroom() == 1.0     # 額度未知 → 可以打

    def test_clear_when_plenty(self):
        lim = lld.HeaderLimiter()
        lim.update(_headers(11963))
        assert lim.headroom() > 0.0

    def test_wall_when_low(self):
        lim = lld.HeaderLimiter()
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            lim.update(_headers(500, reset="30s"))
            assert lim.headroom() == 0.0
            assert lim.reset_secs() == pytest.approx(30.0, abs=0.2)

    def test_clear_once_reset_window_passed(self):
        lim = lld.HeaderLimiter()
        clock = {"t": 1000.0}
        with patch.object(lld.time, "monotonic", side_effect=lambda: clock["t"]):
            lim.update(_headers(500, reset="10s"))
            clock["t"] = 1030.0
            assert lim.headroom() == 1.0

    def test_never_sleeps(self):
        lim = lld.HeaderLimiter()
        with patch.object(lld.time, "monotonic", return_value=1000.0), \
             patch.object(lld.time, "sleep") as sleep:
            lim.update(_headers(10, reset="50s"))
            lim.headroom()
            lim.reset_secs()
            sleep.assert_not_called()                 # 限速器絕不自己等


class TestConcurrencyStress:
    def test_no_crash_or_deadlock_under_load(self):
        """64 執行緒 × 500 次同時 update()/headroom()(模擬 ⌘S 並發猛打)→ 不崩、不死鎖。"""
        lim = lld.HeaderLimiter()
        errors = []

        def worker(wid):
            try:
                for j in range(500):
                    lim.update(_headers((wid * 37 + j * 11) % 13000, reset="20ms"))
                    lim.headroom()
            except Exception as e:        # noqa: BLE001 — 任何例外都算測試失敗
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(64)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"執行緒丟出例外:{errors[:3]}"
        assert all(not t.is_alive() for t in threads), "有執行緒卡住(疑似死鎖)"

    def test_dispatcher_stops_before_429(self):
        """headroom 見底時 wall_secs()>0,呼叫端(⌘S)在真撞 429 前就停。"""
        class _P:
            name = "groq"
            def __init__(self, lim):
                self._lim = lim
            def headroom(self):
                return self._lim.headroom()
            def reset_secs(self):
                return self._lim.reset_secs()
            def generate(self, prompt, **kw):
                return "ok"

        lim = lld.HeaderLimiter()
        d = lld.Dispatcher([_P(lim)])
        with patch.object(lld.time, "monotonic", return_value=1000.0):
            assert d.wall_secs() == 0.0
            lim.update(_headers(500, reset="40s"))    # 低於 floor
            assert d.wall_secs() == pytest.approx(40.0, abs=0.2)
```

- [ ] **Step 2: 跑改造後的測試（此時應已綠——它只依賴 Task 1 的子模組）**

Run: `uv run pytest test_groq_limiter.py -q`
Expected: `15 passed`（parametrize 展開後）

- [ ] **Step 3: 改 `addon/__init__.py` — 匯入與 dispatcher 建立**

在檔頭 `from aqt.utils import showWarning, tooltip`（第 25 行附近）之後加：

```python
from . import _llm_dispatch as _lld

# 雙 provider 分流(KEEP-IN-SYNC 鏡像;無 .gemini_key 自動退化為單 Groq)
_dispatcher = _lld.Dispatcher(
    [p for p in (_lld.GroqProvider.load(), _lld.GeminiProvider.load()) if p])
```

刪除第 30-32 行的 `GROQ_API_URL` / `GROQ_MODEL` / `GROQ_KEY_PATH` 三個常數（已住進子模組），與第 61-68 行的 `_load_groq_key()`。

- [ ] **Step 4: 移除舊 limiter 區、改寫 `_groq_chat`**

刪除 141-190 行區塊：`_parse_int`、`_parse_reset_secs`、`class _GroqLimiter`、`_groq_limiter = _GroqLimiter()`。

`_groq_chat`（193-219）整個換成下面的版本。同區的 `_AddonRateLimited` **保留原地**（它是呼叫端協定）；`_parse_retry_after` **刪除**（已住進子模組）：

```python
def _groq_chat(prompt, *, temperature, max_tokens, timeout, strict=False):
    """One LLM text call via the dual-provider dispatcher; '' on no key / failure.
    strict=True surfaces both-providers-limited as _AddonRateLimited (so the burst
    engine can pace/stop) instead of swallowing it as ''."""
    if not _dispatcher.providers:
        return ""
    try:
        return _dispatcher.generate(prompt, temperature=temperature,
                                    max_tokens=max_tokens, timeout=timeout)
    except _lld.AllProvidersLimited as e:
        if strict:
            raise _AddonRateLimited(int(e.soonest_reset) + 1)
        return ""
    except Exception:
        return ""
```

- [ ] **Step 5: ⌘S 停批閘與撞限訊息**

`BackfillWorker.__init__`（~714-718）加一行：

```python
        self.limit_resets = {}         # 撞牆當下兩家的恢復秒數(對話框訊息用)
```

`_process_one` 的 wall 檢查（~727）：

```python
        wall = _groq_limiter.wall_secs()
```
改為
```python
        wall = _dispatcher.wall_secs()
```
並在 `self.retry_after = max(...)` 之後加：
```python
            self.limit_resets = _dispatcher.resets()
```

`BackfillDialog._on_finished` 撞限分支（~1002-1005）換成：

```python
        if getattr(self._worker, "_hit_limit", False):
            secs = int(self._worker.retry_after)
            resets = getattr(self._worker, "limit_resets", {})
            if len(resets) > 1:        # 雙 provider:報每家真實恢復時間
                self.status.setText(
                    f"Both providers out of quota — {_lld.format_reset_summary(resets)}. "
                    f"Completed {done}, {left} still need filling.")
            else:                      # 單 Groq 退化:沿用原措辭
                self.status.setText(
                    f"Hit the cloud rate limit — completed {done}, {left} still need "
                    f"filling. Try again in ~{secs}s, then reselect.")
```

- [ ] **Step 6: 消費者 grep 驗證**

Run:
```bash
grep -rn "_GroqLimiter\|_groq_limiter\|_load_groq_key\|GROQ_API_URL" --include="*.py" . | grep -v ".venv" | grep -v "_llm_dispatch"
```
Expected: 無輸出（僅子模組內部持有這些概念；`_parse_int`/`_parse_reset_secs`/`_parse_retry_after` 同樣只剩子模組與其測試）

- [ ] **Step 7: 跑全套測試**

Run: `uv run pytest -q`
Expected: 全綠，`180 passed`（151 − 15 舊 limiter 測試 + 29 子模組 + 15 改造後 limiter 測試）。特別確認 `test_backfill_remove.py`、`test_test_cards.py`（stub import addon，會真的執行 `from . import _llm_dispatch`）仍綠。

- [ ] **Step 8: 文件同步**

- `README.md:16` 的「目前僅 CLI / core 批次腳本走這套分流，Anki Addon（⌘A/⌘S）仍是單 Groq」改為「CLI / core 與 Anki Addon（⌘A/⌘S/批量面板）都走這套分流」。
- `README.md` addon 檔案說明表（~220 行）「LLM 用 urllib 直呼 Groq」改為「LLM 經 `addon/_llm_dispatch.py`（Groq+Gemini 分流鏡像）以 urllib 直呼」。
- `CLAUDE.md` Key Rules：
  - 「Addon 用 urllib 直呼 Groq（非 groq SDK）→ 一定要帶 `User-Agent: AnkiWordAdder/1.0` header」改為「Addon 的 LLM 呼叫走 `addon/_llm_dispatch.py`（urllib、非 SDK；Groq 必帶 `User-Agent: AnkiWordAdder/1.0`）」。
  - 韌性層規則行（36-39 行）「addon 尚未接（Phase 2）→ 現況見 README 架構段」改為「addon 走鏡像子模組 `addon/_llm_dispatch.py`（KEEP-IN-SYNC 對照 core 兩檔；自足 stdlib-only → pytest 直接檔案載入測，改動要同步雙邊）」。

- [ ] **Step 9: Commit**

```bash
git add addon/__init__.py test_groq_limiter.py README.md CLAUDE.md
git commit -m "feat: addon 接上 Groq+Gemini 分流(⌘S 停批閘看兩家,撞限訊息帶恢復時間)"
```

---

## 驗收（對照 spec 成功標準）

1. `uv run pytest -q` 全綠（~172）。
2. 使用者手動（**需重啟 Anki**）：⌘A 加字正常；Test Cards 產卡 → ⌘S 補完正常；拿掉 `.gemini_key` 重啟 → 一切照舊。
3. `addon/__init__.py` 淨變瘦（limiter/HTTP 內裡移出）。
