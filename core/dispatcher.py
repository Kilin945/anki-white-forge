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
        ranked = sorted(((p.headroom(), p) for p in self.providers),
                        key=lambda t: t[0], reverse=True)     # headroom 快照一次
        for h, p in ranked:
            if h <= 0.0:
                continue
            if not self._breakers[p.name].allows():           # 試探閘在「真的要打」前才問
                continue
            try:
                text = p.generate(prompt, temperature=temperature, max_tokens=max_tokens)
                self._breakers[p.name].record_success()
                return text
            except ProviderError as e:
                cooldown = getattr(e, "retry_after", None)
                self._breakers[p.name].record_failure(cooldown)
            except Exception:
                self._breakers[p.name].record_failure()   # 非預期例外也要釋放試探閘
                raise
        raise AllProvidersLimited(self._resets())
