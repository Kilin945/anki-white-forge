"""Capacity-aware dispatcher over multiple LLM providers.

路由：每筆呼叫挑 headroom（剩餘額度比例）最大且斷路器放行的 provider。
斷路器：連續失敗 threshold 次 → OPEN（冷卻 = 該家 reset 時間，拿不到用預設）→
冷卻過後 HALF-OPEN 放一筆試探。failover：選中的失敗 → 依序換下一家。
兩家都不可用 → raise AllProvidersLimited（帶每家 reset 秒數，政策由呼叫端決定）。
"""
import re
import threading
import time

from core.providers import ProviderError

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")   # 留 \t \n \r


def clean_llm_text(text):
    """去掉 LLM 回覆裡的控制字元（實測 Groq 回過 'penguin on ice\\x00'）。換行與 tab 保留：
    句子守門靠換行擋洩漏。KEEP-IN-SYNC: core/dispatcher.py 與 addon/_llm_dispatch.py 各一份。"""
    return _CONTROL_CHAR_RE.sub("", text) if text else text


class CircuitBreaker:
    """三態斷路器：CLOSED →（連續失敗達 threshold）→ OPEN →（冷卻到期）→ HALF-OPEN。"""

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
            self._probe_inflight = False
            self._streak += 1
            if self._streak >= self._threshold:
                secs = cooldown if cooldown is not None else self._cooldown_default
                self._open_until = time.monotonic() + float(secs)


class AllProvidersLimited(Exception):
    """所有 provider 都不可用（額度見底/熔斷/失敗）。resets: 每家幾秒後恢復。"""

    def __init__(self, resets):
        super().__init__("all providers limited")
        self.resets = dict(resets)
        self.soonest_reset = min(self.resets.values()) if self.resets else 60.0


class Dispatcher:
    """依任務分池：每筆呼叫只在該任務的池裡挑 headroom 最大且斷路器放行的模型，
    失敗依序換池內下一個；池全滅 raise AllProvidersLimited（只帶該池的 reset）。
    傳 list 等於兩個任務共用同一串（舊介面）。
    KEEP-IN-SYNC: addon/_llm_dispatch.py::Dispatcher（差異：core 無 timeout、無 wall_secs）。"""

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

    def generate(self, prompt, *, temperature=0.7, max_tokens=200, effort="low", task="light"):
        ranked = sorted(((p.headroom(), p) for p in self._pool(task)),
                        key=lambda t: t[0], reverse=True)     # headroom 快照一次；穩定排序：同分照池內順序
        for h, p in ranked:
            if h <= 0.0:
                continue
            if not self._breakers[p.name].allows():           # 試探閘在「真的要打」前才問
                continue
            try:
                text = p.generate(prompt, temperature=temperature, max_tokens=max_tokens,
                                  effort=effort)
                self._breakers[p.name].record_success()
                return clean_llm_text(text)
            except ProviderError as e:
                cooldown = getattr(e, "retry_after", None)
                self._breakers[p.name].record_failure(cooldown)
            except Exception:
                self._breakers[p.name].record_failure()   # 非預期例外也要釋放試探閘
                raise
        raise AllProvidersLimited(self.resets(task))
