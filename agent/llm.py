#!/usr/bin/env python3
"""LLM client for the agent's think step (UM-44).

Talks to any OpenAI-compatible `/chat/completions` endpoint (function/tool
calling) through agent/httpjson.py — no extra pip dependency, per CONTRIBUTING.md's
"no external framework" rule and `docs/AGENT-DIRECTION.md`'s "free models
only" constraint: the primary target is FreeLLMAPI
(https://github.com/tashfeenahmed/freellmapi), which fronts free
OpenRouter/OpenCode models behind exactly this API shape; the fallback is a
local llama.cpp/Ollama/vLLM server, also OpenAI-compatible. `LLM_BASE_URL`/
`LLM_API_KEY`/`LLM_MODEL` (agent/config.py) point this client at whichever
one is configured — this module doesn't care which.

The system prompt is built from the perception snapshot
(agent.perception.WorldState.snapshot) plus the action catalog
(agent.actions.catalog(), one JSON-schema "tool" per registered Action) so
adding a new Action automatically extends what the model can choose from.
"""

import json
import logging
import random
import time

from . import httpjson, prompts
from .httpjson import retry_after_seconds as _retry_after_seconds  # noqa: F401 (tests, #454)

log = logging.getLogger("agent.llm")

DEFAULT_TIMEOUT_S = 20.0
# UM-94: fallback/cooldown policy for a comma-separated LLM_MODEL list.
DEFAULT_MAX_ATTEMPTS = 3              # requests per choose_action() call, across models
DEFAULT_RATE_LIMIT_COOLDOWN_S = 60.0  # 429 with no Retry-After / reset hint
DEFAULT_ERROR_COOLDOWN_S = 30.0       # 5xx, timeout, network error, non-JSON body
MIN_COOLDOWN_S = 5.0
MAX_COOLDOWN_S = 900.0
COOLDOWN_JITTER = 0.1  # a cooldown ends 0-10 % later, so a fleet does not retry in step (#636)

# Prompt text lives in agent/prompts/ (#395).

# UM-90: prompt-size caps. The full snapshot keeps up to 40 of each bucket
# (perception.WorldState.snapshot) and prompts ran 4-8k tokens every 15 s,
# which the free LLM tier rate-limits; the nearest few are what matters.
PROMPT_BUCKET_CAPS = {"nearby_units": 15, "nearby_players": 10, "nearby_objects": 10}


def _prune(value):
    """Drop None/""/False/[]/{} dict entries recursively (list elements are
    kept in place; only their own dict fields are pruned)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = _prune(v)
            if v is None or v is False or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [_prune(v) for v in value]
    return value


def _round_positions(value):
    """Round x/y/z of every position-shaped dict to 0.1 yd."""
    if isinstance(value, dict):
        out = {k: _round_positions(v) for k, v in value.items()}
        if "x" in out and "y" in out:
            for axis in ("x", "y", "z"):
                if isinstance(out.get(axis), float):
                    out[axis] = round(out[axis], 1)
        return out
    if isinstance(value, list):
        return [_round_positions(v) for v in value]
    return value


def compact_snapshot(snapshot: dict) -> dict:
    """The snapshot as sent to the model (UM-90): nearest N per bucket,
    positions rounded, empty/null/false fields dropped. The audit log keeps
    the full snapshot; this only shapes the prompt."""
    out = dict(snapshot)
    for key, cap in PROMPT_BUCKET_CAPS.items():
        if isinstance(out.get(key), list):
            out[key] = out[key][:cap]
    return _prune(_round_positions(out))


class LLMError(Exception):
    """The LLM call failed, or returned something that isn't a usable tool
    call (network error, bad status, missing/malformed tool_calls, etc).
    Callers should treat this as "skip this think cycle", not crash the
    agent loop."""


class LLMUnavailable(LLMError):
    """The request never produced a usable reply: HTTP error, timeout,
    network error or a non-JSON body. `retryable` errors (404, 429, 5xx,
    timeouts, network) put the model on cooldown and fall through to the
    next model in the list; others (400, 401, 403...) are configuration
    problems and are raised as-is."""

    def __init__(self, message: str, status: int | None = None,
                 retry_after: float | None = None, retryable: bool = True):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.retryable = retryable

    def short(self) -> str:
        return f"HTTP {self.status}" if self.status is not None else str(self)[:120]


def build_tools(catalog: list[dict]) -> list[dict]:
    """Wrap agent.actions.catalog()'s `{name, description, parameters}`
    entries in the OpenAI/FreeLLMAPI `{"type": "function", "function": {...}}`
    tool-list shape."""
    return [{"type": "function", "function": schema} for schema in catalog]


def build_messages(snapshot: dict, persona: str = "", history: list | None = None) -> list[dict]:
    """System prompt (rules + standing goal + optional persona) and one user
    message carrying the recent action history (UM-90) and the compacted
    perception snapshot. The history is the only memory between cycles;
    it comes from agent.think.ThinkState, not from chat turns."""
    system = prompts.system_prompt(persona)
    parts = []
    if history:
        parts.append("history:\n" + "\n".join(json.dumps(h, default=str, separators=(",", ":"))
                                                for h in history))
    parts.append("snapshot:\n" + json.dumps(compact_snapshot(snapshot), default=str, separators=(",", ":")))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(parts)},
    ]


class LLMClient:
    """Thin OpenAI-compatible `/chat/completions` client. One instance is
    reused across think cycles; its only state besides config is the
    per-model cooldown table and the last call's usage/latency/model.

    `model` may be one model id or a comma-separated, ordered fallback list
    (UM-94), e.g. "gemini-3.1-flash-lite,gemini-3.6-flash,auto". Each
    choose_action() call tries the models in order, skipping any that are
    cooling down. A model that fails with HTTP 429, HTTP 5xx, a timeout or a
    network error is put on cooldown (the gateway's Retry-After header or
    "Soonest reset ~Ns" hint, else a default, clamped to
    [MIN_COOLDOWN_S, MAX_COOLDOWN_S]) and the next model is tried, up to
    `max_attempts` requests per call. When every model is cooling down,
    choose_action() raises LLMError without sending a request, so a
    rate-limited agent stops burning a doomed call every think cycle."""

    def __init__(self, base_url: str, model: str, api_key: str = "",
                 timeout: float = DEFAULT_TIMEOUT_S,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS, clock=time.monotonic,
                 rng: random.Random | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model  # the raw configured string, as logged at startup
        self.models = parse_model_list(model)
        self.api_key = api_key
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        # model id -> clock() time at which it may be tried again.
        self._cooldown_until: dict[str, float] = {}
        # Populated by the last choose_action() call, for callers (agent.think's
        # audit logging, UM-51) that want token usage/latency without changing
        # choose_action()'s (name, params) return shape. Reset at the start of
        # every call, so a failed cycle reports {}/None rather than the
        # previous cycle's numbers (UM-94). `last_model` is the model that
        # actually answered: the response's own `model` field when the
        # gateway reports one (FreeLLMAPI's "auto" routes to a concrete
        # model), else the id we requested. Never holds the API key or
        # anything else that needs redacting.
        self.last_usage: dict = {}
        self.last_latency_ms: float | None = None
        self.last_model: str | None = None
        self.last_prompt_id: str | None = None  # agent.prompts ids of the last request (#395)

    def _post(self, path: str, body: dict) -> dict:
        try:
            # error_body_limit: the first 500 bytes of an error body feed both the
            # message and the "Soonest reset" hint (UM-94).
            return httpjson.post_json(f"{self.base_url}{path}", body, api_key=self.api_key,
                                      timeout=self.timeout, error_body_limit=500)  # type: ignore[return-value]
        except httpjson.HttpJsonError as e:
            if e.kind == "http":
                # 404: FreeLLMAPI's "model not found or removed upstream" for a
                # stale catalog entry (seen for gemini-2.5-flash in the UM-94
                # probe) — specific to that model, so fall through to the next.
                status = e.status or 0
                raise LLMUnavailable(f"HTTP {status} from {e.url}: {(e.body or b'')!r}",
                                     status=e.status, retry_after=e.retry_after,
                                     retryable=status in (404, 429) or 500 <= status < 600) from None
            if e.kind == "network":
                raise LLMUnavailable(f"request to {e.url} failed: {e.detail}") from None
            raise LLMUnavailable(f"non-JSON response from {e.url}: {(e.body or b'')[:500]!r}") from None

    def cooldowns(self) -> dict[str, float]:
        """Seconds left on each model's cooldown (only models still cooling)."""
        now = self._clock()
        return {m: until - now for m, until in self._cooldown_until.items() if until > now}

    def _cool_down(self, model: str, err: "LLMUnavailable") -> float:
        if err.retry_after is not None:
            seconds = err.retry_after
        elif err.status == 404:
            seconds = MAX_COOLDOWN_S  # stale catalog entry; won't come back soon
        elif err.status == 429:
            seconds = DEFAULT_RATE_LIMIT_COOLDOWN_S
        else:
            seconds = DEFAULT_ERROR_COOLDOWN_S
        seconds = min(max(seconds, MIN_COOLDOWN_S), MAX_COOLDOWN_S)
        seconds *= 1 + self._rng.uniform(0, COOLDOWN_JITTER)
        self._cooldown_until[model] = self._clock() + seconds
        return seconds

    def choose_action(self, snapshot: dict, catalog: list[dict],
                       persona: str = "", history: list | None = None) -> tuple[str, dict]:
        """One chat-completions call with the action catalog as tools.
        Returns (action_name, params) for the single tool call the model
        made. Raises LLMError for anything that isn't exactly one valid
        tool call — the caller (agent.think) treats that as "skip this
        cycle", never as a crash.

        With a fallback list, a 429/5xx/timeout from one model cools it down
        and the next available model is tried (at most `max_attempts`
        requests). A reply that arrives but isn't a usable tool call is not
        retried on another model: that's the model's choice, not an outage."""
        self.last_usage = {}
        self.last_latency_ms = None
        self.last_model = None
        self.last_prompt_id = None
        if not self.models:
            raise LLMError("no LLM model configured")

        messages = build_messages(snapshot, persona=persona, history=history)
        self.last_prompt_id = prompts.prompt_id(persona)
        tools = build_tools(catalog)
        failures: list[str] = []
        attempts = 0
        started = self._clock()
        for model in self.models:
            if attempts >= self.max_attempts:
                break
            # Don't start another attempt once one timeout's worth of time has
            # gone by: two slow failures then a third try would stall the
            # think loop for up to max_attempts * timeout (FreeLLMAPI's
            # gemini-3.5-flash timed out at 30s in the UM-94 probe).
            if attempts and self._clock() - started >= self.timeout:
                break
            if self._cooldown_until.get(model, 0.0) > self._clock():
                continue
            attempts += 1
            body = {
                "model": model,
                "messages": messages,
                "tools": tools,
                "tool_choice": "required",
            }
            t0 = time.monotonic()
            try:
                response = self._post("/chat/completions", body)
            except LLMUnavailable as e:
                if not e.retryable:
                    raise
                seconds = self._cool_down(model, e)
                log.warning("llm model %s unavailable (%s); cooling down %.0fs",
                            model, e.short(), seconds)
                failures.append(f"{model}: {e.short()}")
                continue
            self.last_latency_ms = (time.monotonic() - t0) * 1000.0
            if not isinstance(response, dict):
                raise LLMError(f"malformed response from {model}: {response!r}")
            self.last_usage = response.get("usage") or {}
            routed = response.get("model")
            self.last_model = routed if isinstance(routed, str) and routed else model
            if failures:
                log.info("llm fell back to %s after: %s", self.last_model, "; ".join(failures))
            return _extract_tool_call(response)

        cooling = self.cooldowns()
        msg = (f"all {attempts} attempt(s) failed: " + "; ".join(failures)
               if failures else "no request sent")
        if all(m in cooling for m in self.models):
            soonest = min(self.models, key=lambda m: cooling[m])
            msg += (f"; all models cooling down, soonest {soonest} "
                    f"in {cooling[soonest]:.0f}s")
        raise LLMError(msg)


def parse_model_list(model: str) -> list[str]:
    """"a, b,,c" -> ["a", "b", "c"]; duplicates dropped, order kept."""
    out: list[str] = []
    for part in (model or "").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part)
    return out


def _extract_tool_call(response: dict) -> tuple[str, dict]:
    try:
        choices = response["choices"]
        message = choices[0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"malformed response, no choices[0].message: {response!r}") from e

    tool_calls = message.get("tool_calls") or []
    if not tool_calls:
        raise LLMError(f"model did not call a tool: {message!r}")
    if len(tool_calls) > 1:
        log.warning("model returned %d tool calls, using the first: %r",
                    len(tool_calls), tool_calls)

    call = tool_calls[0]
    try:
        function = call["function"]
        name = function["name"]
        raw_args = function.get("arguments", "{}")
    except (KeyError, TypeError) as e:
        raise LLMError(f"malformed tool_call: {call!r}") from e

    if isinstance(raw_args, dict):
        params = raw_args
    else:
        try:
            params = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError as e:
            raise LLMError(f"tool_call arguments is not valid JSON: {raw_args!r}") from e

    if not isinstance(params, dict):
        raise LLMError(f"tool_call arguments must decode to an object: {raw_args!r}")

    return name, params
