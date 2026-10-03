# jev-mock

Local, stdlib-only stand-in for OpenRouter's Decisions API, the endpoint that
serves Jev (`typesafe/jev-1.13`). It lets the agent's `JevClient` (UM-99) run
in dev and CI without an `OPENROUTER_API_KEY` or network cost (ADR 0001, UM-96).

The policy is deliberately random: each `choice` question gets a uniformly
random option key, with a random probability distribution in which that key is
the most likely, and a confidence derived from how concentrated it is. It only
exercises the HTTP contract; the answers mean nothing.

## Run

```bash
python3 tools/jev-mock/server.py            # http://127.0.0.1:8090
JEV_BASE_URL=http://127.0.0.1:8090/api/alpha  # point the agent at it
```

Note that `JEV_BASE_URL` includes `/api/alpha`: `JevClient` posts to
`{JEV_BASE_URL}/decisions`. Any API key, or none, is accepted unless
`JEV_MOCK_REQUIRE_AUTH=1`.

With the agents, in Docker (dev only: the answers are random, so never point
live agents at it). The service is in `docker-compose.agents.yml` under its own
`jev-mock` profile, and `JEV_BASE_URL` stays empty unless you set it:

```bash
JEV_BASE_URL=http://jev-mock:8090/api/alpha \
  docker compose -f docker-compose.agents.yml --profile jev-mock up -d jev-mock agent-luaprata
```

| Variable | Default | Purpose |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | Bind address. Use `0.0.0.0` inside a container. |
| `PORT` | `8090` | Listen port. |
| `JEV_MOCK_SEED` | unset | Seed the RNG for reproducible answers. |
| `JEV_MOCK_STATUS` | unset | Answer every decisions request with this error status, e.g. `402` (out of credits) or `429` (rate limited). |
| `JEV_MOCK_REQUIRE_AUTH` | unset | `1`: 401 without `Authorization: Bearer ...`, like the real API. |

## Contract

`POST /api/alpha/decisions`, shape from OpenRouter's Decisions API reference
and Jev tutorial (checked 2026-10-02):

```json
{"model": "typesafe/jev-1.13", "state": {...},
 "questions": {"next_action": {"type": "choice", "instructions": "...",
                               "criteria": {"attack(guid=4660)": "...", "rest()": "..."}}}}
```

```json
{"id": "gen-dec-mock-...", "model": "typesafe/jev-1.13", "provider": "jev-mock",
 "answers": {"next_action": {"type": "choice", "choice": "rest()", "confidence": 0.31,
                             "probabilities": {"attack(guid=4660)": 0.27, "rest()": 0.73}}},
 "usage": {"input_tokens": 120, "output_tokens": 0, "cost": 0.0}}
```

Errors use OpenRouter's body, `{"error": {"code": <status>, "message": "..."}}`:
400 for a malformed request (including `noul`/`score` questions, which the
mock does not implement), 404 for other paths, 413 over 1 MiB.
`GET /healthz` returns `{"ok": true}`.

## Test

```bash
python3 -m unittest discover -s tools/jev-mock/tests -v
```

The `JevClient*` tests drive the real `agent.jev.JevClient` against the mock
in-process. `tests/test_external.py` does the same against a mock running as
its own process, and is skipped unless `JEV_MOCK_URL` is set (CI sets it):

```bash
python3 tools/jev-mock/server.py &
JEV_MOCK_URL=http://127.0.0.1:8090/api/alpha python3 -m unittest -v tools/jev-mock/tests/test_external.py
```
