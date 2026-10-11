"""The one token-budget implementation (#454, #213): a rolling-hour cap on
input+output tokens, `AGENT_MAX_TOKENS_PER_HOUR`, 0 = unlimited.

Several deciders may share one `TokenBudget` object; they then spend from the
same allowance. The caller asks `exhausted()` before a call and `record()`s the
tokens the call billed.
"""

import time
from collections import deque
from collections.abc import Callable

WINDOW_S = 3600.0


class TokenBudget:
    def __init__(self, max_tokens_per_hour: int = 0, clock: Callable[[], float] = time.monotonic):
        self.max_tokens_per_hour = max(0, int(max_tokens_per_hour or 0))
        self._clock = clock
        self._spend: deque[tuple[float, int]] = deque()  # (clock time, tokens) per billed call

    def _prune(self) -> None:
        cutoff = self._clock() - WINDOW_S
        while self._spend and self._spend[0][0] <= cutoff:
            self._spend.popleft()

    def used(self) -> int:
        self._prune()
        return sum(t for _, t in self._spend)

    def exhausted(self, who: str = "jev") -> str | None:
        """None while the window has room (or the budget is off); otherwise the
        reason `who` must not be called."""
        if not self.max_tokens_per_hour:
            return None
        used = self.used()
        if used < self.max_tokens_per_hour:
            return None
        return (f"{who} token budget exhausted: {used} of {self.max_tokens_per_hour} "
                f"tokens used in the last hour (AGENT_MAX_TOKENS_PER_HOUR)")

    def record(self, tokens: int, at: float | None = None) -> None:
        """Count `tokens` billed now (or at clock time `at`). Non-positive counts are ignored."""
        if tokens > 0:
            self._spend.append((self._clock() if at is None else at, tokens))

    def preload(self, entries: list[tuple[float, int]]) -> None:
        """Add earlier spend (clock time, tokens), e.g. restored from the audit log
        after a restart. They go before anything recorded so far."""
        self._spend.extendleft(sorted(entries, reverse=True))
