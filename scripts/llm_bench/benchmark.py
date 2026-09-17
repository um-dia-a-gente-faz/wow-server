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
"""

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
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


@dataclass
class CandidateReport:
    name: str
    calls: list = field(default_factory=list)

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

        return {
            "candidate": self.name,
            "n_calls": n,
            "http_ok_rate": round(len(ok_calls) / n, 3) if n else 0.0,
            "valid_tool_call_rate": round(len(valid) / n, 3) if n else 0.0,
            "family_match_rate": round(len(family) / n, 3) if n else 0.0,
            "latency_p50_s": pct(0.50, latencies),
            "latency_p95_s": pct(0.95, latencies),
            "status_counts": statuses,
        }


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
                return CallResult(
                    ok=True,
                    latency_s=latency,
                    status=resp.status,
                    tool_name=name,
                    tool_args=args,
                    valid=name is not None,
                    family_match=name in expected if name else False,
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
    import random

    expected = prompt.get("expected_tool_family", ["wait"])
    latency = random.uniform(0.4, 2.5)
    time.sleep(0.01)  # keep --mock runs fast regardless of "latency"
    if random.random() < 0.85:
        name = random.choice(expected)
    else:
        name = random.choice(list(TOOL_NAMES))
    return CallResult(ok=True, latency_s=latency, status=200, tool_name=name, valid=True, family_match=name in expected)


def run_candidate(candidate: dict, prompts: list, concurrency: int, mock: bool, timeout: float, retries: int) -> CandidateReport:
    report = CandidateReport(name=candidate["name"])
    tasks = prompts * max(1, concurrency)
    fn = (lambda p: mock_call_once(p)) if mock else (lambda p: call_once(candidate, p, timeout, retries))
    if concurrency > 1 and not mock:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            report.calls = list(pool.map(fn, tasks))
    else:
        report.calls = [fn(p) for p in tasks]
    return report


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, help="JSON file with a list of candidates (see module docstring)")
    ap.add_argument("--replay-set", type=Path, default=DEFAULT_REPLAY_SET)
    ap.add_argument("--concurrency", type=int, default=1, help="Simulated concurrent agents per candidate")
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
        report = run_candidate(candidate, prompts, args.concurrency, args.mock, args.timeout, args.retries)
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
                    "error": c.error,
                }
                for c in r.calls
            ]
            for r in all_reports
        }
        args.json_out.write_text(json.dumps(full, indent=2))


if __name__ == "__main__":
    main()
