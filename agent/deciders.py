#!/usr/bin/env python3
"""The Decider interface (#278): what agent/brain.py calls on a decision model.

`Brain` fills its jev slot with a JevClient and its llm slot with an LLMClient.
Both answer `choose_action(snapshot, second, *, persona="", history=None)`,
where `second` is the candidate list for Jev and the action catalog for the
LLM. Brain omits `history` from the LLM call when it is None, so every
decider accepts both call shapes.

`ScriptedDecider` is the deterministic implementation: no network, no sleep.
It backs the tests and live smoke runs that must not spend tokens.
"""

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Protocol

from .jev import JevError

if TYPE_CHECKING:
    from .jev import JevClient
    from .llm import LLMClient


class Decider(Protocol):
    """Structural type of a decision model as Brain uses it. `last_confidence`
    (jev slot only) and `skipped_single_candidate` are further optional
    readouts Brain reads with getattr(), so they are not members here."""

    @property
    def model(self) -> str | None: ...

    @property
    def last_usage(self) -> dict: ...

    @property
    def last_latency_ms(self) -> float | None: ...

    def choose_action(self, snapshot: dict, candidates_or_catalog: list[dict], *,
                      persona: str = "", history: list | None = None) -> tuple[str, dict]: ...


Rule = Callable[[dict, list[dict]], tuple[str, dict]]


class ScriptedDecider:
    """Replays `(action, params)` pairs in order, then repeats the last one.
    Given a callable instead, returns `rule(snapshot, options)`.

    An empty list raises JevError when `fail_when_empty` is set (the decision
    is missing, as a failed Jev call would be); otherwise it chooses `idle`."""

    model = "scripted"

    def __init__(self, script: Sequence[tuple[str, dict]] | Rule, *,
                 fail_when_empty: bool = False):
        self._script = script
        self._fail_when_empty = fail_when_empty
        self._next = 0
        self.last_usage: dict = {}
        self.last_latency_ms: float | None = 0.0
        self.last_confidence: float | None = 1.0

    def choose_action(self, snapshot: dict, candidates_or_catalog: list[dict], *,
                      persona: str = "", history: list | None = None) -> tuple[str, dict]:
        self.last_usage = {}
        if callable(self._script):
            action, params = self._script(snapshot, candidates_or_catalog)
            return action, dict(params)
        if not self._script:
            if self._fail_when_empty:
                raise JevError("scripted decider has no decisions left")
            return "idle", {}
        action, params = self._script[min(self._next, len(self._script) - 1)]
        self._next += 1
        return action, dict(params)


if TYPE_CHECKING:  # mypy proves every Brain client satisfies Decider
    def _conforms(jev: "JevClient", llm: "LLMClient", scripted: ScriptedDecider) -> list[Decider]:
        return [jev, llm, scripted]
