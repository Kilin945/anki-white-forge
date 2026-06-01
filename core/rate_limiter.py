"""Reusable batch throttle / 429 brake for rate-limited API loops (e.g. Groq).

Pure batch accounting — no Anki or field knowledge. Import from any batch job.
"""


class RateLimitReached(Exception):
    """Raised by API helpers when the provider returns HTTP 429."""


def is_rate_limit_error(exc):
    """True if exc represents HTTP 429 (urllib HTTPError, groq RateLimitError, etc.)."""
    if isinstance(exc, RateLimitReached):
        return True
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code == 429:
        return True
    return "429" in str(exc)


class BatchLimiter:
    """Counts successful calls; stops a batch at batch_limit or on rate-limit."""

    def __init__(self, batch_limit=25):
        self.batch_limit = batch_limit
        self.processed = 0
        self.stopped_reason = None   # None | "batch_limit" | "rate_limited"

    def should_continue(self):
        if self.stopped_reason:
            return False
        if self.processed >= self.batch_limit:
            self.stopped_reason = "batch_limit"
            return False
        return True

    def record_success(self):
        self.processed += 1

    def record_rate_limited(self):
        self.stopped_reason = "rate_limited"
