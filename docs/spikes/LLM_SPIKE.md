# LLM benchmark spike (UM-61)

> **Archived spike.** Outcome: pinned free tool-calling model primary, local llama.cpp/Ollama fallback; the later Jev design is in `docs/adr/0001-jev-in-the-think-loop.md`. Issue: UM-61. Superseded by: ADR 0001 for the decision path.

Timeboxed spike for UM-44's think loop: which free LLM should decide agent
actions, and how many agents can one shared model server support at
UM-63's 5/25-agent scale? Constraints from `docs/AGENT-DIRECTION.md` §3:
free models only, one model server per GPU machine (not one model per
agent), think interval 10–30 s.

**Verdict up front:** primary = a pinned, tool-calling-capable free
OpenRouter model behind FreeLLMAPI (not `auto`); fallback = a local
llama.cpp/Ollama server on the laptop's RTX 4060-class 8 GB GPU. See
[Recommendation](#recommendation).

## Why this is a research spike, not a live benchmark

UM-44 (the think loop, `agent/brain/`, `--think-dry`) has not landed yet —
`agent/` has no `brain/` package to generate the 30–50 recorded prompts the
card asks for. This spike instead built a **synthetic replay set** shaped
identically to `docs/AI-AGENT-SPEC.md`'s perception JSON and UM-44's planned
tool-call format (`scripts/llm_bench/replay_set.json`, 33 prompts across
combat, following, chat reply, party invite, quest accept/turn-in/abandon,
inventory, idle, and a few robustness/edge cases), plus a runnable harness
(`scripts/llm_bench/benchmark.py`).

The harness could not be exercised against real traffic in this environment:

- No FreeLLMAPI or OpenRouter API key is available here (both require a
  dashboard/account signup — out of scope for a code change, and the brief
  says ask before installing software or provisioning access on the owner's
  machines).
- The candidate local-model hosts (laptop, desktop, Pandora) are the
  owner's physical machines; this environment has no GPU and can't reach
  them to install/run llama.cpp or Ollama.
- `192.168.1.60:3002` (FreeLLMAPI) was reachable from this environment and
  answered `401 Invalid API key` — reachability confirmed, credentials were
  not. That instance was removed on 2026-10-03 (#133); FreeLLMAPI now runs
  at `192.168.1.72:3001` (LXC 301 `freellmapi` on pv1).

So: the **script is ready to run** (validated end-to-end with `--mock`, and
against the real OpenRouter endpoint where it correctly reports `401`s
without a key). The **candidate list and hardware sizing below are
research-based** (current OpenRouter free-tier catalog, published
llama.cpp/Ollama throughput figures, and the hardware table already in
`docs/AGENT-DIRECTION.md`), not measured live. Running the real benchmark
(§ [How to run it for real](#how-to-run-it-for-real)) is the immediate
follow-up, filed as UM-66/UM-67 below.

## Candidates

### FreeLLMAPI (primary path)

FreeLLMAPI (https://github.com/tashfeenahmed/freellmapi) proxies to
OpenRouter's free tier (and others) behind a single OpenAI-compatible
endpoint (`192.168.1.72:3001`; it was `192.168.1.60:3002` when this spike
was written), which is the integration UM-44 already
targets (`agent/config.py`'s `LLM_BASE_URL`/`LLM_MODEL`, `.env.example`).

Earlier prototype note (an old handoff note, cited in UM-44, no longer in the repo): `auto` routing
gave **~50% valid tool-call rate** — not enough for UM-44's ≥90% bar. `auto`
picks whichever free model is available that moment, including ones with
weak or no tool-call support, so the fix is to **pin specific models**
rather than route through `auto`.

Querying OpenRouter's live model catalog (`GET /api/v1/models`, run during
this spike) for free models that advertise `tools` in
`supported_parameters`:

| Model | Context | Tool-calling advertised |
|---|---|---|
| `z-ai/glm-5.2:free` | 32,768 | **No** |
| `nvidia/nemotron-3.5-content-safety:free` | 128,000 | **No** |
| `google/gemma-4-26b-a4b-it:free` | 262,144 | Yes |
| `google/gemma-4-31b-it:free` | 262,144 | Yes |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | 256,000 | Yes |
| `nvidia/nemotron-3-super-120b-a12b:free` | 262,144 | Yes |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | 1,000,000 | Yes |
| `nex-agi/nex-n2.5-mini:free` | 262,144 | Yes |
| `nex-agi/nex-n2.5-pro:free` | 262,144 | Yes |
| `cohere/north-mini-code:free` | 256,000 | Yes |
| `thinkingmachines/inkling-small:free` | 1,048,576 | Yes |
| `poolside/laguna-s-2.1:free` | 262,144 | Yes |

(Full list of 20 free models and the raw catalog snapshot: rerun
`curl https://openrouter.ai/api/v1/models` — the catalog changes over time,
so pin by re-checking `supported_parameters` before locking a model in.)

`supported_parameters` advertising `tools` is a necessary but not
sufficient signal — it means the API *accepts* a `tools` array, not that
the model reliably *calls* one. That gap is exactly what
`scripts/llm_bench/benchmark.py`'s `valid_tool_call_rate` measures and this
spike could not run live. Pick 3–5 of the above (favor smaller/faster
ones first — `gemma-4-26b-a4b-it`, `nemotron-3-nano-omni-30b-a3b-reasoning`,
`nex-n2.5-mini` — since think-loop prompts are short and latency matters
more than raw capability) and run `benchmark.py` against them before
pinning one in `LLM_MODEL`.

### Local models (fallback path)

Sized against the hardware table already in `docs/AGENT-DIRECTION.md` §3,
using published llama.cpp/Ollama throughput ballparks for Q4_K_M
quantization (not measured on this hardware):

| Machine | GPU | Model size that fits | Expected single-stream throughput (Q4, published figures) |
|---|---|---|---|
| Laptop (Core Ultra 7, 64 GB) | RTX 4060-class, 8 GB | 7–8B (e.g. Llama 3.1 8B, Qwen2.5 7B) | ~40–70 tok/s |
| Desktop (i5-14600KF, 32 GB) | RTX 4070 SUPER, 12 GB | up to ~14B (e.g. Qwen2.5 14B) | ~35–55 tok/s, less while WoW client is running |
| Pandora (Ryzen 5 5600H, Proxmox) | RTX 3050 Ti, 4 GB | 3–4B (e.g. Qwen2.5 3B, Phi-3.5-mini) | ~50–90 tok/s (small model, more headroom) |

Candidates to actually pull and benchmark: **Qwen2.5-7B-Instruct** and
**Llama-3.1-8B-Instruct** (both have solid llama.cpp/Ollama tool-call
templates) on the laptop; **Qwen2.5-14B-Instruct** on the desktop as extra
capacity; **Qwen2.5-3B-Instruct** on Pandora if agent processes ever need a
same-box model.

Local serving options, in order of setup simplicity: **Ollama** (easiest,
built-in OpenAI-compatible `/v1/chat/completions`, automatic model pull) →
**llama.cpp `server`** (more control over batching/context, still simple)
→ **vLLM** (best throughput under concurrency via continuous batching, but
heavier to install and mainly pays off with more VRAM than these GPUs have).
For 8 GB/12 GB single-GPU boxes serving one agent host with think intervals
of 10–30 s (not a high-QPS API), Ollama or llama.cpp's own batching is
enough; vLLM's advantage shows up at concurrency levels these GPUs can't
reach anyway (a 7–8B model's KV cache for 25 concurrent short contexts
already competes with the 8 GB budget before compute does).

## Method (as run and as intended)

1. **Replay set** — `scripts/llm_bench/replay_set.json`: 33 prompts,
   `{system, user, expected_tool_family}` per case, covering the categories
   UM-61 lists (combat, following, chat reply, party invite, quest
   decisions, "nothing to do") plus a few robustness cases (malformed/null
   perception fields, mob far above level, chat cooldown active).
2. **Tool catalog** — `scripts/llm_bench/tools_catalog.py`: a stand-in for
   UM-36/UM-44's real action registry (not built yet), shaped after the
   action catalog in `docs/AI-AGENT-SPEC.md` so the same JSON-schema
   validation UM-44's think loop (`agent/think.py`) will need (exactly one tool call, known
   name, required args present) gets exercised here.
3. **Harness** — `scripts/llm_bench/benchmark.py`: stdlib-only
   (`urllib`, per `CONTRIBUTING.md`), OpenAI-compatible
   `/v1/chat/completions` with `tools`, retries with backoff on 429/5xx,
   `--concurrency N` to fire N parallel "agents" replaying the set (proxy
   for 5/10/25 concurrent agents against one shared model server), and a
   `--mock` mode with no network calls to validate the script and replay
   set in isolation.
4. **Metrics per candidate**: HTTP-ok rate (surfaces 429s/cooldowns),
   valid-tool-call rate, family-match rate (called tool ∈
   `expected_tool_family` — a cheap automatic proxy; the
   sensible/acceptable/wrong rubric UM-61 asks for still needs a human or a
   stronger judge model to grade each transcript, which is out of scope for
   this timebox), latency p50/p95.
5. What could actually be run here: `--mock` end-to-end (33×5 = 165 calls,
   confirms parsing/validation/reporting/concurrency all work) and one real
   call against `https://openrouter.ai/api/v1/chat/completions` with no key,
   confirming the script surfaces `401`s cleanly instead of crashing.
   Neither produces a real quality/latency number — see the warning in the
   script's own docstring and `--mock`'s summary field.

## How to run it for real

From a machine with an OpenRouter (or FreeLLMAPI) API key, or LAN access to
a running local-model server:

```bash
cp scripts/llm_bench/candidates.json.example scripts/llm_bench/candidates.json
# edit candidates.json: pin the 3-5 OpenRouter free models above, plus any
# local servers already running (base_url per docs/AGENT-DIRECTION.md hosts)
export OPENROUTER_API_KEY=...      # or FREELLMAPI_API_KEY
python3 scripts/llm_bench/benchmark.py --config scripts/llm_bench/candidates.json --concurrency 1
python3 scripts/llm_bench/benchmark.py --config scripts/llm_bench/candidates.json --concurrency 5   # party-size load
python3 scripts/llm_bench/benchmark.py --config scripts/llm_bench/candidates.json --concurrency 25  # raid-size load
```

`candidates.json` is a local, untracked file (only the `.example` is in the repo), gitignored-by-convention the same way `.env` is (don't
commit real endpoints/keys beyond the `.example` file). Add
`--json-out results.json` to keep the raw per-call data for the
sensible/acceptable/wrong rubric pass.

For the laptop's 1-hour sustained-load check (tokens/s over time, GPU temp,
throttling) that UM-61 also asks for: run `--concurrency 5` in a loop for
an hour while sampling `nvidia-smi --query-gpu=temperature.gpu,utilization.gpu,power.draw --format=csv -l 5` (or the vendor equivalent) on the laptop
itself; this environment has no GPU to do that here.

## Recommendation

- **Primary:** FreeLLMAPI, pinned to a specific tool-calling-capable free
  OpenRouter model (start with `google/gemma-4-26b-a4b-it:free` or
  `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` — mid-size, tool
  support advertised, generous context for the <4k-token prompts UM-44
  budgets for). **Never `auto`** — that's the documented ~50% tool-call
  failure mode.
- **Fallback:** a local OpenAI-compatible server (Ollama, simplest to
  stand up) on the **laptop** (RTX 4060-class, 8 GB), running a 7–8B
  Q4 model (Qwen2.5-7B-Instruct or Llama-3.1-8B-Instruct). This matches
  `AGENT-DIRECTION.md`'s existing designation of the laptop as "main local
  model server during play sessions" and keeps the desktop free for the
  owner's own WoW client.
- **Think interval:** keep at the direction doc's 10–30 s band; start at
  20 s. At FreeLLMAPI's free-tier rate limits, 5 agents thinking every 20 s
  is ~15 requests/min, 25 agents is ~75 requests/min — both plausible to
  hit 429s on a shared free key, which is exactly what
  `benchmark.py`'s status-count field is for; widen the interval (towards
  30 s) if the real run shows sustained 429s at 25-agent load.
- **Max agent count per setup** (estimate, pending the real benchmark run):
  - FreeLLMAPI primary: bounded by the provider's free-tier rate limit, not
    local hardware — likely fine for 5 (party), needs the real 25-agent
    request-rate test to confirm before committing to a 25-agent raid on
    this path alone.
  - Local fallback on the laptop, 7–8B Q4, one request at a time per
    UM-44's per-agent LLM call: with 20–30 s think intervals and single-GPU
    serialized inference (~1–3 s per short decision at the throughput
    figures above), the laptop can comfortably queue and answer **5
    agents** (party) with room to spare; **10–20 agents** is plausible but
    needs the real concurrency test to confirm queueing latency doesn't
    creep past the think interval; treat **25 concurrent agents on the
    laptop alone** as optimistic until measured — the desktop's 4070 SUPER
    is the natural second host to split load for a full 25-agent raid.
- **Split for 25 agents:** run the party's 5 agents on FreeLLMAPI (primary)
  as today, and stand up the local laptop model as fallback + overflow
  capacity for the rest of the raid, matching `AGENT-DIRECTION.md`'s "one
  model server per GPU machine, shared by all agents" rule. Revisit once
  UM-63's roster exists and the real 25-agent load test can run against it.

## Follow-ups

- Run `scripts/llm_bench/benchmark.py` for real once an OpenRouter/
  FreeLLMAPI key is available and UM-44 lands (swap the synthetic replay
  set for real `--think-dry` transcripts) — fill in the results table
  above with measured numbers instead of research estimates.
- Stand up Ollama on the laptop as the local fallback server (needs the
  owner's go-ahead to install software on that machine, per this spike's
  brief).
- Run the 1-hour sustained-load / thermal check on the laptop.
- File the "run llama.cpp/Ollama server on the other PC" setup issue once
  the above is confirmed (UM-61's acceptance criteria asks for this
  explicitly).
