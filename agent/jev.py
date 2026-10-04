#!/usr/bin/env python3
"""Jev client for the agent's think step (UM-99, ADR 0001 / UM-95).

Jev (TypeSafe's decision model, served by OpenRouter as `typesafe/jev-1.13`
or `~typesafe/jev-latest`) does not generate text or arguments: it picks one
option out of a set we supply. The candidate generator (UM-97) builds that set
from the snapshot as concrete `{"action": str, "params": dict}` entries; this
client asks Jev which one, and returns that candidate's `(action, params)`.
Since the answer can only be one of the keys we sent, a hallucinated action
name or a mangled GUID cannot happen.

Wire format: `POST {JEV_BASE_URL}{JEV_PATH}`, bearer auth with JEV_API_KEY.
`JEV_PATH` defaults to `/decisions` (agent/config.py). The base URL is always
explicit; the OpenRouter base below is only used for JEV_PROVIDER=openrouter.
Two providers share this shape: OpenRouter's Decisions API (base
`https://openrouter.ai/api/alpha`, path `/decisions`, model
`typesafe/jev-1.13`, OpenRouter key) and TypeSafe's native API (base
`https://api.typesafe.ai`, path `/v1/systemone`, model `jev-latest`, TypeSafe
key; GH-195). Field names follow OpenRouter's Jev tutorial and Decisions
API reference (checked 2026-10-02, not yet against a real call):

    request:  {"model", "state", "questions": {name: {"type": "choice",
               "instructions", "criteria": {option_key: description}}}}
    response: {"model", "answers": {name: {"type": "choice", "choice",
               "probabilities"?, "confidence"?}}, "usage": {"input_tokens",
               "output_tokens", "cost"?}}
    error:    {"error": {"code", "message"}} with a non-2xx status

stdlib `urllib` only, like agent/llm.py.
"""

import http.client
import json
import time
import urllib.error
import urllib.request

from agent.llm import compact_snapshot

OPENROUTER_BASE_URL = "https://openrouter.ai/api/alpha"  # JEV_PROVIDER=openrouter only
DEFAULT_PATH = "/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_TIMEOUT_S = 20.0

QUESTION = "next_action"

INSTRUCTIONS = """You control a World of Warcraft (3.3.5a) character. \
"state.snapshot" is what the character perceives right now; "state.history" \
lists its recent actions and their results, oldest first. Pick the one action \
to take next. Goal: level up efficiently on your own (quests, fighting mobs \
close to your level, looting, resting when health or mana is low). Do not \
repeat an action that just failed or changed nothing."""


class JevError(Exception):
    """The Jev call failed or returned no usable choice (network error, bad
    status, malformed response, a choice we did not offer, no candidates).
    Like agent.llm.LLMError, callers treat it as "skip this think cycle".
    `status` is the HTTP status when the server answered with one (402 = out
    of credits, 429 = rate limited), else None."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def candidate_key(candidate: dict) -> str:
    """The criteria key Jev sees and answers with. The candidate's own `id`
    when it has one (UM-97's stable ids), else `action(k=v,...)` built from
    the params with sorted keys, so the same candidate always gets the same
    key."""
    cid = candidate.get("id")
    if cid:
        return str(cid)
    params = candidate.get("params") or {}
    args = ",".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{candidate['action']}({args})"


def candidate_description(candidate: dict) -> str:
    """The criteria value: the candidate's `description`/`label` when given,
    else the action and params as JSON."""
    for field in ("description", "label"):
        if candidate.get(field):
            return str(candidate[field])
    return json.dumps({"action": candidate["action"], "params": candidate.get("params") or {}},
                      default=str, sort_keys=True, separators=(",", ":"))


def build_criteria(candidates: list[dict]) -> dict[str, dict]:
    """Map criteria key -> candidate. Raises JevError on an empty list, a
    malformed candidate, or two candidates sharing a key (Jev could not tell
    them apart)."""
    if not candidates:
        raise JevError("no candidates to choose from")
    by_key: dict[str, dict] = {}
    for c in candidates:
        if not isinstance(c, dict) or not isinstance(c.get("action"), str) \
                or not isinstance(c.get("params", {}), dict):
            raise JevError(f"malformed candidate: {c!r}")
        key = candidate_key(c)
        if key in by_key:
            raise JevError(f"duplicate candidate key {key!r}")
        by_key[key] = c
    return by_key


def build_request(model: str, snapshot: dict, by_key: dict[str, dict],
                  persona: str = "", history: list | None = None) -> dict:
    """The Decisions API body: one `choice` question over the candidates.
    The snapshot is compacted the same way as the LLM prompt (UM-90)."""
    state = {"snapshot": compact_snapshot(snapshot)}
    if history:
        state["history"] = history
    instructions = INSTRUCTIONS
    if persona:
        instructions += f"\nPersona: {persona}"
    return {
        "model": model,
        "state": state,
        "questions": {
            QUESTION: {
                "type": "choice",
                "instructions": instructions,
                "criteria": {k: candidate_description(c) for k, c in by_key.items()},
            },
        },
    }


class JevClient:
    """Decisions API client. Same call shape as
    agent.llm.LLMClient.choose_action, except the second argument is the
    candidate list instead of the action catalog. One instance is reused
    across think cycles."""

    def __init__(self, base_url: str, model: str = DEFAULT_MODEL,
                 api_key: str = "", timeout: float = DEFAULT_TIMEOUT_S,
                 path: str = DEFAULT_PATH):
        self.base_url = base_url.rstrip("/")
        self.path = "/" + (path or DEFAULT_PATH).lstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        # Set by every choose_action() call, for audit logging (UM-51); the
        # same role as LLMClient.last_usage/last_latency_ms. Never holds the key.
        self.last_usage: dict = {}
        self.last_latency_ms: float | None = None
        self.last_choice: str | None = None
        self.last_confidence: float | None = None
        self.last_probabilities: dict = {}

    def _reset_last(self):
        self.last_usage = {}
        self.last_latency_ms = None
        self.last_choice = None
        self.last_confidence = None
        self.last_probabilities = {}

    @property
    def url(self) -> str:
        return f"{self.base_url}{self.path}"

    def _post(self, body: dict) -> dict:
        url = self.url
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, data=json.dumps(body, default=str).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise JevError(f"HTTP {e.code} from {url}: {_error_message(e)}", status=e.code) from e
        except urllib.error.URLError as e:
            raise JevError(f"request to {url} failed: {e.reason}") from e
        except (OSError, http.client.HTTPException) as e:
            # Timeouts / dropped connections while reading the body are not
            # wrapped in URLError (same trap as UM-81 in agent/llm.py).
            raise JevError(f"request to {url} failed: {e}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise JevError(f"non-JSON response from {url}: {raw[:500]!r}") from e
        if not isinstance(data, dict):
            raise JevError(f"response is not a JSON object: {raw[:500]!r}")
        return data

    def choose_action(self, snapshot: dict, candidates: list[dict],
                      persona: str = "", history: list | None = None) -> tuple[str, dict]:
        """Ask Jev which candidate to execute and return its
        `(action, params)`. A single candidate is returned without a network
        call (nothing to decide, and Jev bills per input token). Raises
        JevError for anything else that does not yield one offered
        candidate."""
        self._reset_last()
        by_key = build_criteria(candidates)
        if len(by_key) == 1:
            (key, only), = by_key.items()
            self.last_choice, self.last_confidence = key, 1.0
            self.last_probabilities = {key: 1.0}
            return only["action"], dict(only.get("params") or {})

        body = build_request(self.model, snapshot, by_key, persona=persona, history=history)
        t0 = time.monotonic()
        response = self._post(body)
        self.last_latency_ms = (time.monotonic() - t0) * 1000.0
        usage = response.get("usage")
        self.last_usage = usage if isinstance(usage, dict) else {}

        try:
            answer = response["answers"][QUESTION]
            choice = answer["choice"]
        except (KeyError, TypeError) as e:
            raise JevError(f"malformed response, no answers.{QUESTION}.choice: "
                           f"{str(response)[:500]}") from e
        if not isinstance(choice, str) or choice not in by_key:
            raise JevError(f"Jev chose {choice!r}, which is not an offered candidate")

        self.last_choice = choice
        confidence = answer.get("confidence")
        self.last_confidence = float(confidence) if isinstance(confidence, (int, float)) else None
        probs = answer.get("probabilities")
        self.last_probabilities = probs if isinstance(probs, dict) else {}
        chosen = by_key[choice]
        return chosen["action"], dict(chosen.get("params") or {})


def _error_message(e: urllib.error.HTTPError) -> str:
    """`error.message` from an OpenRouter error body, else the raw body."""
    try:
        raw = e.read()
    except (OSError, http.client.HTTPException):
        return "(no body)"
    try:
        return str(json.loads(raw)["error"]["message"])[:500]
    except (ValueError, KeyError, TypeError):
        return repr(raw[:500])
