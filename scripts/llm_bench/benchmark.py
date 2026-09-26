#!/usr/bin/env python3
"""LLM benchmark for the agent think-loop (UM-61 spike).

Sends scripts/llm_bench/replay_set.json — synthetic perception->action
prompts shaped like docs/AI-AGENT-SPEC.md and UM-44's planned prompt format
— to one or more OpenAI-compatible /v1/chat/completions endpoints with
`tools`, and reports per candidate:

  - valid tool-call rate (parses as exactly one tool call, matching a known
    tool name and required arguments)
  - family match rate (the called tool is in the prompt's
    `expected_tool_family` — a soft proxy for decision quality; a human
    rubric pass is still needed for the "sensible / acceptable / wrong"
    scoring UM-61 asks for)
  - latency p50/p95
  - error/HTTP-status counts (429s and cooldowns show up here)

Stdlib only (urllib), per CONTRIBUTING.md. No SDKs, no external deps.

Usage:
    # Requires network access + valid credentials; nothing is bundled.
    python3 scripts/llm_bench/benchmark.py --config scripts/llm_bench/candidates.json

    # Sanity-check the script and replay set without any network calls or
    # credentials — exercises parsing/validation/reporting against a mock
    # responder.
    python3 scripts/llm_bench/benchmark.py --mock

Candidates config (JSON list), one entry per model to benchmark:
    [
      {"name": "openrouter/glm-5.2-air:free",
       "base_url": "https://openrouter.ai/api/v1",
       "api_key_env": "OPENROUTER_API_KEY",
       "model": "z-ai/glm-5.2-air:free"},
      {"name": "local-llama-cpp-8b",
       "base_url": "http://192.168.1.61:8080/v1",
       "api_key_env": "",
       "model": "local"}
    ]

Concurrency: pass --concurrency N to fire N requests in parallel per
candidate, simulating N agents thinking at once against a shared model
server (see UM-61's 5/10/25-agent throughput requirement). Each concurrent
"agent" replays the same prompt set independently.

Two load shapes, and they answer different questions (UM-61):

  --concurrency N   Saturation. N requests in flight at all times, as fast
                    as the server answers them. Good for peak tokens/s and
                    for finding the breaking point; NOT what the agents
                    actually do.
  --agents N        Paced arrival, which is the real workload.
    --think-interval S
                    Each of N virtual agents wakes every S seconds (±jitter),
                    issues one think call, and sleeps until its next slot —
                    docs/AGENT-DIRECTION.md §3's "every 10-30 s, not every
                    3 s". This is the shape that decides how many agents a
                    machine sustains, because it measures whether a think
                    step still finishes inside its own window once N agents
                    share one server.

The sustained-agent-count rule this harness reports on (`sustainable`):
a configuration passes when valid_tool_call_rate >= 0.90 AND
latency_p95_s <= 0.5 * think_interval. The p95 half-window margin leaves
room for the perception/act half of the cycle and for the owner's WoW
client stealing GPU mid-session; a think step that eats its whole window
means the agent is permanently late, not merely slow.
"""

import argparse
import concurrent.futures
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tools_catalog import TOOLS, TOOL_NAMES  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_REPLAY_SET = HERE / "replay_set.json"


@dataclass
class CallResult:
    ok: bool
    latency_s: float
    status: int = 200
    tool_name: str | None = None
    tool_args: dict | None = None
    valid: bool = False
    family_match: bool = False
    error: str = ""
    # Token accounting, from the response's OpenAI-compatible `usage` block.
    # llama.cpp, vLLM and OpenRouter all populate it; if a provider omits it
    # these stay 0 and the tokens/s fields report None rather than a wrong
    # number (never infer tokens from response length).
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # Paced mode only (--agents): how late this call started relative to the
    # agent's scheduled think slot. Non-zero means the harness itself, not
    # the server, fell behind — it invalidates the latency number.
    schedule_slip_s: float = 0.0


@dataclass
class CandidateReport:
    name: str
    calls: list = field(default_factory=list)
    # Load shape this report was produced under, echoed into the summary so a
    # pasted number can never be read as the wrong workload.
    load: dict = field(default_factory=dict)
    wall_s: float = 0.0

    def summarize(self) -> dict:
        n = len(self.calls)
        ok_calls = [c for c in self.calls if c.ok]
        latencies = sorted(c.latency_s for c in ok_calls)
        valid = [c for c in self.calls if c.valid]
        family = [c for c in self.calls if c.family_match]
        statuses = {}
        for c in self.calls:
            statuses[c.status] = statuses.get(c.status, 0) + 1

        def pct(k, arr):
            if not arr:
                return None
            idx = min(len(arr) - 1, int(round(k * (len(arr) - 1))))
            return round(arr[idx], 3)

        # tokens/s, two different and both useful numbers:
        #   per_call  — decode speed the model achieves on one stream. This is
        #               the "tokens/s" a model card quotes.
        #   aggregate — completion tokens the whole server emitted per second
        #               of wall clock. This is what capacity planning needs,
        #               and under concurrency it is the larger of the two.
        timed = [c for c in ok_calls if c.completion_tokens and c.latency_s > 0]
        per_call_tps = sorted(c.completion_tokens / c.latency_s for c in timed)
        total_completion = sum(c.completion_tokens for c in ok_calls)
        total_prompt = sum(c.prompt_tokens for c in ok_calls)
        have_usage = total_completion > 0

        interval = (self.load or {}).get("think_interval_s")
        p95 = pct(0.95, latencies)
        valid_rate = round(len(valid) / n, 3) if n else 0.0
        # See the module docstring: >=90% valid tool calls AND p95 inside half
        # the think window. Only meaningful for paced (--agents) runs, so it is
        # reported as None for saturation runs rather than as a misleading bool.
        sustainable = None
        if interval and p95 is not None:
            sustainable = bool(valid_rate >= 0.90 and p95 <= 0.5 * interval)

        summary = {
            "candidate": self.name,
            "load": self.load,
            "n_calls": n,
            "wall_s": round(self.wall_s, 1) if self.wall_s else None,
            "http_ok_rate": round(len(ok_calls) / n, 3) if n else 0.0,
            "valid_tool_call_rate": valid_rate,
            "family_match_rate": round(len(family) / n, 3) if n else 0.0,
            "latency_p50_s": pct(0.50, latencies),
            "latency_p95_s": p95,
            "tokens_per_s_per_call_p50": pct(0.50, per_call_tps) if have_usage else None,
            "tokens_per_s_aggregate": (
                round(total_completion / self.wall_s, 1)
                if have_usage and self.wall_s > 0 else None
            ),
            "completion_tokens_total": total_completion if have_usage else None,
            "prompt_tokens_total": total_prompt if have_usage else None,
            "status_counts": statuses,
            "sustainable": sustainable,
        }
        if self.load.get("mode") == "paced":
            slips = sorted(c.schedule_slip_s for c in self.calls)
            summary["harness_schedule_slip_p95_s"] = pct(0.95, slips)
            summary["overrun_rate"] = (
                round(sum(1 for c in ok_calls if c.latency_s > interval) / len(ok_calls), 3)
                if ok_calls and interval else None
            )
        return summary


def load_replay_set(path: Path) -> list:
    with open(path) as f:
        return json.load(f)


def build_request(base_url: str, api_key: str, model: str, system: str, user: str) -> urllib.request.Request:
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": TOOLS,
            "tool_choice": "required",
            "temperature": 0.2,
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    url = base_url.rstrip("/") + "/chat/completions"
    return urllib.request.Request(url, data=body, headers=headers, method="POST")


def parse_tool_call(response_json: dict):
    """Return (tool_name, tool_args) or (None, None) if no valid single tool
    call is present. Mirrors the validation UM-44's loop.py will need:
    exactly one tool call, known name, required args present.
    """
    try:
        choice = response_json["choices"][0]
        message = choice["message"]
        tool_calls = message.get("tool_calls") or []
    except (KeyError, IndexError, TypeError):
        return None, None
    if len(tool_calls) != 1:
        return None, None
    call = tool_calls[0]
    fn = call.get("function", {})
    name = fn.get("name")
    if name not in TOOL_NAMES:
        return None, None
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except json.JSONDecodeError:
        return None, None
    schema = next(t["function"] for t in TOOLS if t["function"]["name"] == name)
    required = schema["parameters"].get("required", [])
    if any(r not in args for r in required):
        return None, None
    return name, args


def call_once(candidate: dict, prompt: dict, timeout: float, retries: int) -> CallResult:
    base_url = candidate["base_url"]
    api_key = os.environ.get(candidate.get("api_key_env", ""), "")
    model = candidate["model"]
    system = prompt["system"]
    user = prompt["user"]
    expected = set(prompt.get("expected_tool_family", []))

    backoff = 1.0
    last_error = ""
    last_status = 0
    for attempt in range(retries + 1):
        req = build_request(base_url, api_key, model, system, user)
        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                latency = time.monotonic() - start
                data = json.loads(resp.read().decode("utf-8"))
                name, args = parse_tool_call(data)
                usage = data.get("usage") or {}
                return CallResult(
                    ok=True,
                    latency_s=latency,
                    status=resp.status,
                    tool_name=name,
                    tool_args=args,
                    valid=name is not None,
                    family_match=name in expected if name else False,
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                )
        except urllib.error.HTTPError as e:
            latency = time.monotonic() - start
            last_status = e.code
            last_error = e.reason
            if e.code in (429, 500, 502, 503) and attempt < retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return CallResult(ok=False, latency_s=latency, status=e.code, error=str(e.reason))
        except urllib.error.URLError as e:
            latency = time.monotonic() - start
            last_error = str(e.reason)
            if attempt < retries:
                time.sleep(backoff)
                backoff *= 2
                continue
            return CallResult(ok=False, latency_s=latency, status=0, error=last_error)
    return CallResult(ok=False, latency_s=0.0, status=last_status, error=last_error)


# ─── Mock mode (no network / no credentials) ──────────────────────────────
# Lets anyone sanity-check the script and replay set — parsing, validation,
# reporting, concurrency — before running it with real endpoints and keys.
# It does NOT produce benchmark numbers; --mock output must never be quoted
# as a real latency/quality result.

def mock_call_once(prompt: dict) -> CallResult:
    expected = prompt.get("expected_tool_family", ["wait"])
    latency = random.uniform(0.4, 2.5)
    time.sleep(0.01)  # keep --mock runs fast regardless of "latency"
    if random.random() < 0.85:
        name = random.choice(expected)
    else:
        name = random.choice(list(TOOL_NAMES))
    return CallResult(
        ok=True, latency_s=latency, status=200, tool_name=name, valid=True,
        family_match=name in expected,
        # Plausible-shaped usage so --mock also exercises the tokens/s math.
        # --mock output is never a real benchmark number; see the warning above.
        prompt_tokens=random.randint(700, 1100),
        completion_tokens=random.randint(20, 60),
    )


def run_candidate(candidate: dict, prompts: list, concurrency: int, mock: bool, timeout: float, retries: int) -> CandidateReport:
    """Saturation shape: `concurrency` requests in flight continuously."""
    report = CandidateReport(
        name=candidate["name"],
        load={"mode": "saturation", "concurrency": max(1, concurrency)},
    )
    tasks = prompts * max(1, concurrency)
    fn = (lambda p: mock_call_once(p)) if mock else (lambda p: call_once(candidate, p, timeout, retries))
    t0 = time.monotonic()
    if concurrency > 1 and not mock:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            report.calls = list(pool.map(fn, tasks))
    else:
        report.calls = [fn(p) for p in tasks]
    report.wall_s = time.monotonic() - t0
    return report


def run_candidate_paced(candidate: dict, prompts: list, agents: int, interval: float,
                        duration: float, jitter: float, mock: bool, timeout: float,
                        retries: int) -> CandidateReport:
    """Paced shape: `agents` virtual agents each think once per `interval`
    seconds (+/- jitter), for `duration` seconds, against one shared server.

    This is the workload docs/AGENT-DIRECTION.md §3 actually describes, and
    the only shape whose latency answers "does a think step finish inside its
    window with N agents running". Each agent walks the prompt set in a
    different rotation so the server is not answering N copies of the same
    prompt from cache.
    """
    report = CandidateReport(
        name=candidate["name"],
        load={
            "mode": "paced",
            "agents": agents,
            "think_interval_s": interval,
            "duration_s": duration,
            "jitter_s": jitter,
        },
    )
    call = (lambda p: mock_call_once(p)) if mock else (lambda p: call_once(candidate, p, timeout, retries))
    start = time.monotonic()
    deadline = start + duration
    results: list = []
    lock = threading.Lock()

    def agent_loop(agent_idx: int) -> None:
        rng = random.Random(1000 + agent_idx)
        # Stagger the fleet across the interval instead of having all N agents
        # fire on the same second — real agents start at different times, and a
        # synchronized fleet measures a thundering herd, not steady state.
        slot = start + (interval * agent_idx / max(1, agents))
        k = 0
        while True:
            target = slot + k * interval + rng.uniform(-jitter, jitter)
            now = time.monotonic()
            if target >= deadline:
                return
            if target > now:
                time.sleep(target - now)
            began = time.monotonic()
            if began >= deadline:
                return
            prompt = prompts[(agent_idx + k) % len(prompts)]
            res = call(prompt)
            res.schedule_slip_s = max(0.0, began - target)
            with lock:
                results.append(res)
            k += 1

    threads = [threading.Thread(target=agent_loop, args=(i,), daemon=True)
               for i in range(agents)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=duration + timeout + 30)
    report.wall_s = time.monotonic() - start
    report.calls = results
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="JSON file with a list of candidates (see module docstring)")
    ap.add_argument("--replay-set", type=Path, default=DEFAULT_REPLAY_SET)
    ap.add_argument("--concurrency", type=int, default=1,
                    help="Saturation load: N requests in flight at all times")
    ap.add_argument("--agents", type=int, default=0,
                    help="Paced load: N virtual agents thinking every --think-interval "
                         "seconds for --duration seconds. This is the real agent "
                         "workload; overrides --concurrency when set.")
    ap.add_argument("--think-interval", type=float, default=20.0,
                    help="Seconds between one agent's think calls in --agents mode "
                         "(docs/AGENT-DIRECTION.md §3 band: 10-30, default 20)")
    ap.add_argument("--duration", type=float, default=180.0,
                    help="Seconds to run --agents mode")
    ap.add_argument("--jitter", type=float, default=2.0,
                    help="Random +/- seconds applied to each think slot in --agents mode")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--mock", action="store_true", help="No network calls; validates the script/replay-set only")
    ap.add_argument("--json-out", type=Path, help="Write full per-call results as JSON to this path")
    args = ap.parse_args()

    prompts = load_replay_set(args.replay_set)

    if args.mock:
        candidates = [{"name": "mock-candidate"}]
    else:
        if not args.config:
            ap.error("--config is required unless --mock is set")
        with open(args.config) as f:
            candidates = json.load(f)

    all_reports = []
    for candidate in candidates:
        if args.agents > 0:
            report = run_candidate_paced(
                candidate, prompts, args.agents, args.think_interval,
                args.duration, args.jitter, args.mock, args.timeout, args.retries,
            )
        else:
            report = run_candidate(candidate, prompts, args.concurrency, args.mock,
                                   args.timeout, args.retries)
        all_reports.append(report)

    summaries = [r.summarize() for r in all_reports]
    print(json.dumps(summaries, indent=2))

    if args.json_out:
        full = {
            r.name: [
                {
                    "ok": c.ok,
                    "latency_s": round(c.latency_s, 3),
                    "status": c.status,
                    "tool_name": c.tool_name,
                    "valid": c.valid,
                    "family_match": c.family_match,
                    "prompt_tokens": c.prompt_tokens,
                    "completion_tokens": c.completion_tokens,
                    "schedule_slip_s": round(c.schedule_slip_s, 3),
                    "error": c.error,
                }
                for c in r.calls
            ]
            for r in all_reports
        }
        args.json_out.write_text(json.dumps(full, indent=2))


if __name__ == "__main__":
    main()
