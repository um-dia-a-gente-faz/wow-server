#!/usr/bin/env bash
# Local OpenAI-compatible model server for the agents' think step (UM-61).
#
# One server per GPU machine, shared by every agent — docs/AGENT-DIRECTION.md
# §3, "not one model per agent". This wraps llama.cpp's `llama-server`, which
# exposes /v1/chat/completions with tool calling, so agent/llm.py talks to it
# with no code change: point LLM_BASE_URL at it.
#
#   scripts/llm-serve.sh start [model-key]   start (default model key: qwen3-8b)
#   scripts/llm-serve.sh stop                stop
#   scripts/llm-serve.sh status              is it running, on what
#   scripts/llm-serve.sh health              hit the endpoint and print the model
#   scripts/llm-serve.sh models              list known model keys
#
# Install/rollback: everything lives under $LLM_SERVE_HOME (default
# /home/rubens/.local/share/wow-llm) — a user-local llama.cpp build plus GGUF
# files. No system packages, no sudo, no systemd unit. To roll back completely:
# `scripts/llm-serve.sh stop && rm -rf /home/rubens/.local/share/wow-llm`.
# See docs/MODEL-SERVING.md for the measured numbers behind the defaults.
set -euo pipefail

HOME_DIR="${LLM_SERVE_HOME:-/home/rubens/.local/share/wow-llm}"
BIN_DIR="${LLM_SERVE_BIN:-$HOME_DIR/bin/llama-b11191}"
MODEL_DIR="${LLM_SERVE_MODELS:-$HOME_DIR/models}"
RUN_DIR="$HOME_DIR/run"
LOG_DIR="$HOME_DIR/logs"
PIDFILE="$RUN_DIR/llama-server.pid"
METAFILE="$RUN_DIR/llama-server.meta"

HOST="${LLM_SERVE_HOST:-0.0.0.0}"
PORT="${LLM_SERVE_PORT:-8080}"
# Server slots. Each slot answers one agent's think call at a time; agents are
# paced 10-30 s apart, so slots are reused heavily and 8 covers well past the
# 5-agent party. Total context is split across slots, hence CTX = SLOTS * 3072
# (worst-case think prompt measured at ~1.7k tokens: action catalog + snapshot).
SLOTS="${LLM_SERVE_SLOTS:-8}"
CTX_PER_SLOT="${LLM_SERVE_CTX_PER_SLOT:-3072}"
# q8_0 KV cache roughly halves KV VRAM versus f16 for no measurable quality
# loss on these short prompts. It is what makes an 8B model plus 8 slots fit
# beside the owner's desktop session on a 12 GB card.
CACHE_TYPE="${LLM_SERVE_CACHE_TYPE:-q8_0}"

# ── Known models ──────────────────────────────────────────────────────────
# key|gguf filename|served model name
# Priority order and the measurements behind it: docs/MODEL-SERVING.md.
MODELS="
qwen3-8b|Qwen3-8B-Q4_K_M.gguf|qwen3-8b
lfm2.5-8b-a1b|LFM2.5-8B-A1B-Q4_K_M.gguf|lfm2.5-8b-a1b
qwen3-4b|Qwen3-4B-Q4_K_M.gguf|qwen3-4b
"

die() { echo "error: $*" >&2; exit 1; }

lookup_model() {
  local key="$1" line
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in "$key|"*) echo "$line"; return 0;; esac
  done <<< "$MODELS"
  return 1
}

is_running() {
  [ -f "$PIDFILE" ] || return 1
  local pid; pid="$(cat "$PIDFILE")"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

cmd_models() {
  printf '%-16s %-34s %s\n' KEY GGUF PRESENT
  local line key gguf
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    key="${line%%|*}"; gguf="$(echo "$line" | cut -d'|' -f2)"
    printf '%-16s %-34s %s\n' "$key" "$gguf" \
      "$([ -f "$MODEL_DIR/$gguf" ] && echo yes || echo MISSING)"
  done <<< "$MODELS"
}

cmd_start() {
  local key="${1:-qwen3-8b}" line gguf alias ctx
  line="$(lookup_model "$key")" || die "unknown model key '$key' (try: $0 models)"
  gguf="$(echo "$line" | cut -d'|' -f2)"
  alias="$(echo "$line" | cut -d'|' -f3)"
  [ -f "$MODEL_DIR/$gguf" ] || die "model file missing: $MODEL_DIR/$gguf"
  [ -x "$BIN_DIR/llama-server" ] || die "llama-server not found at $BIN_DIR (see docs/MODEL-SERVING.md § Install)"

  if is_running; then
    echo "already running (pid $(cat "$PIDFILE")): $(cat "$METAFILE" 2>/dev/null || echo unknown)"
    return 0
  fi

  mkdir -p "$RUN_DIR" "$LOG_DIR"
  ctx=$(( SLOTS * CTX_PER_SLOT ))
  local log="$LOG_DIR/llama-server.log"

  # --reasoning off matters for latency, not correctness: Qwen3-class hybrid
  # models otherwise emit a <think> block before the tool call, which triples
  # time-to-decision for a decision that needs no visible chain of thought.
  # --jinja is what makes tool calling work at all (the model's own chat
  # template renders the tools array); without it tool_calls comes back empty.
  LD_LIBRARY_PATH="$BIN_DIR" nohup "$BIN_DIR/llama-server" \
    --model "$MODEL_DIR/$gguf" \
    --alias "$alias" \
    --host "$HOST" --port "$PORT" \
    --ctx-size "$ctx" --parallel "$SLOTS" \
    --n-gpu-layers 99 \
    --cache-type-k "$CACHE_TYPE" --cache-type-v "$CACHE_TYPE" \
    --jinja --reasoning off \
    --metrics \
    >>"$log" 2>&1 &

  echo $! > "$PIDFILE"
  echo "$alias ($gguf) slots=$SLOTS ctx=$ctx kv=$CACHE_TYPE port=$PORT" > "$METAFILE"
  echo "starting: $(cat "$METAFILE")"
  echo "log: $log"

  # Loading an 8B model off NVMe onto the GPU takes a few seconds; don't
  # return success until /health actually answers (never claim a service is
  # healthy because the process exists).
  local i
  for i in $(seq 1 60); do
    if curl -fsS -m 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      echo "healthy after ${i}s"
      cmd_health
      return 0
    fi
    is_running || { echo "process died during startup; last log lines:" >&2; tail -20 "$log" >&2; return 1; }
    sleep 1
  done
  echo "did not become healthy within 60s; last log lines:" >&2
  tail -20 "$log" >&2
  return 1
}

cmd_stop() {
  if ! is_running; then
    echo "not running"
    rm -f "$PIDFILE"
    return 0
  fi
  local pid; pid="$(cat "$PIDFILE")"
  kill "$pid"
  local i
  for i in $(seq 1 20); do
    is_running || { rm -f "$PIDFILE"; echo "stopped (pid $pid)"; return 0; }
    sleep 1
  done
  echo "did not exit after 20s, sending SIGKILL" >&2
  kill -9 "$pid" 2>/dev/null || true
  rm -f "$PIDFILE"
}

cmd_status() {
  if is_running; then
    echo "running (pid $(cat "$PIDFILE")): $(cat "$METAFILE" 2>/dev/null || echo unknown)"
  else
    echo "not running"
    return 1
  fi
}

cmd_health() {
  # The real check: the OpenAI-compatible surface the agents use answers, and
  # reports which model is loaded.
  local out
  out="$(curl -fsS -m 5 "http://127.0.0.1:$PORT/v1/models")" \
    || die "no answer from http://127.0.0.1:$PORT/v1/models"
  echo "$out" | python3 -c 'import json,sys; d=json.load(sys.stdin); print("served model:", ", ".join(m["id"] for m in d.get("data",[])))'
}

case "${1:-}" in
  start)  shift; cmd_start "${1:-}";;
  stop)   cmd_stop;;
  status) cmd_status;;
  health) cmd_health;;
  models) cmd_models;;
  *) echo "usage: $0 {start [model-key]|stop|status|health|models}" >&2; exit 2;;
esac
