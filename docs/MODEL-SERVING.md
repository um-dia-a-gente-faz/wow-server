# Local model serving (UM-61)

`scripts/llm-serve.sh` runs one local OpenAI-compatible model server for the
agents' think step. One server per GPU machine, shared by every agent — the
ownership rule in `docs/AGENT-DIRECTION.md` §3 is "not one model per agent",
so nobody gets their own process.

It wraps llama.cpp's `llama-server`, which serves `/v1/chat/completions` with
tool calling, so `agent/llm.py` talks to it with no code change: point
`LLM_BASE_URL` at it.

## Usage

```sh
scripts/llm-serve.sh start [model-key]   # start (default model key: qwen3-8b)
scripts/llm-serve.sh stop                # stop
scripts/llm-serve.sh status              # is it running, and on what
scripts/llm-serve.sh health              # hit the endpoint, print the model
scripts/llm-serve.sh models              # list known model keys
```

Model keys and the GGUF file each expects under `$MODEL_DIR`:

| key | GGUF | served model name |
|---|---|---|
| `qwen3-8b` (default) | `Qwen3-8B-Q4_K_M.gguf` | `qwen3-8b` |
| `lfm2.5-8b-a1b` | `LFM2.5-8B-A1B-Q4_K_M.gguf` | `lfm2.5-8b-a1b` |
| `qwen3-4b` | `Qwen3-4B-Q4_K_M.gguf` | `qwen3-4b` |

`models` prints a `MISSING` mark for any GGUF not present, which is the fastest
way to find out why a `start` failed.

## Layout

Everything lives under `$LLM_SERVE_HOME` (default `~/.local/share/wow-llm`) —
user-local, no sudo, no systemd unit:

```
$LLM_SERVE_HOME/
  bin/llama-b11191/     llama.cpp build (llama-server + its libraries)
  models/               *.gguf files
  run/                  llama-server.pid, llama-server.meta
  logs/llama-server.log
```

Rollback is `scripts/llm-serve.sh stop && rm -rf ~/.local/share/wow-llm`.

## Configuration (environment)

| Variable | Default | Meaning |
|---|---|---|
| `LLM_SERVE_HOME` | `~/.local/share/wow-llm` | root of the install |
| `LLM_SERVE_BIN` | `$LLM_SERVE_HOME/bin/llama-b11191` | where `llama-server` lives |
| `LLM_SERVE_MODELS` | `$LLM_SERVE_HOME/models` | where the GGUFs live |
| `LLM_SERVE_HOST` | `0.0.0.0` | bind address (agents are other machines) |
| `LLM_SERVE_PORT` | `8080` | port |
| `LLM_SERVE_SLOTS` | `8` | `--parallel` server slots |
| `LLM_SERVE_CTX_PER_SLOT` | `3072` | context per slot; total context is `SLOTS × this` |
| `LLM_SERVE_CACHE_TYPE` | `q8_0` | KV-cache type |

Two flags matter for correctness or latency and are not configurable on
purpose:

- `--jinja` — without it tool calling does not work at all: the model's own chat
  template is what renders the tools array, and `tool_calls` comes back empty.
- `--reasoning off` — Qwen3-class hybrid models otherwise emit a `think` block
  before the decision, roughly tripling time-to-decision for a call that needs
  no visible chain of thought.

`SLOTS × CTX_PER_SLOT` is the total context the server allocates; the defaults
(8 × 3072) are sized for a 5-agent party with agents paced 10-30 s apart,
against a worst-case think prompt measured at ~1.7k tokens (action catalog +
snapshot). The `q8_0` KV cache roughly halves KV memory against `f16` with no
measurable quality difference on prompts this short, which is what lets an 8B
model plus 8 slots fit beside a desktop session on a 12 GB card.

## Pointing an agent at it

```sh
LLM_BASE_URL=http://<gpu-host>:8080/v1
LLM_API_KEY=            # not needed for a local server
LLM_MODEL=qwen3-8b      # must match the served alias
```

Then a normal think cycle exercises it; `LLM_MODEL` accepts a comma-separated
fallback list (UM-94) if you want the local model tried before the free-tier
gateways.

## Measured numbers

The script's own comments refer to measurements behind the defaults — model
priority order, tokens/second, time-to-decision per model. **Those numbers are
not recorded in this repository**: the branch that introduced the script never
carried the document. They come from `scripts/llm_bench/`, which is the way to
reproduce them (its pace-aware load shape exists for exactly this comparison).
If you re-measure, put the numbers here rather than in the script's comments.
