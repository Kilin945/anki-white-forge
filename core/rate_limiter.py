"""Reusable batch throttle / 429 brake for rate-limited API loops (e.g. Groq).

Pure batch accounting — no Anki or field knowledge. Import from any batch job.
"""


class RateLimitReached(Exception):
    """Raised by API helpers when the provider returns HTTP 429.
    retry_after = seconds to wait before retrying (from Retry-After header, else 60)."""
    def __init__(self, retry_after=60):
        super().__init__("rate limited")
        self.retry_after = retry_after


class BatchLimiter:
    """Counts successful calls; stops a batch at batch_limit or on rate-limit.
    batch_limit=None means no cap (run until rate-limited or the work runs out)."""

    def __init__(self, batch_limit=25):
        self.batch_limit = batch_limit
        self.processed = 0
        self.stopped_reason = None   # None | "batch_limit" | "rate_limited"
        self.retry_after = 60        # seconds to wait if stopped by rate limit

    def should_continue(self):
        if self.stopped_reason:
            return False
        if self.batch_limit is not None and self.processed >= self.batch_limit:
            self.stopped_reason = "batch_limit"
            return False
        return True

    def record_success(self):
        self.processed += 1

    def record_rate_limited(self, retry_after=60):
        self.stopped_reason = "rate_limited"
        self.retry_after = retry_after
