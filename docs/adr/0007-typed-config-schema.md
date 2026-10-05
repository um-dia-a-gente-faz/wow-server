# ADR 0007: One typed schema for every agent setting

## Status

Accepted. Records a decision already merged in PR #255 (commit `9ce303f`, GH #250).

## Context

The agent's settings were read from `os.environ` in several modules, and the list
of variables was repeated by hand in `docker-compose.agents.yml` and
`.env.example`. The copies drifted, and a bad value (`AGENT_THINK_INTERVAL_S=abc`)
failed late or silently. Issue #213 was an example of the drift: a documented
spend cap that no code read.

## Decision

`agent/config.py` declares every variable once, in `SETTINGS`: a tuple of frozen
`Setting(name, kind, default, description, secret, compose)`.

- `get(name)` returns the typed value. Unset gives the default; an empty string
  gives the default for int/float/bool and `""` for str (compose passes unset
  variables as empty). An invalid value raises `ConfigError` naming the variable.
- `Config` is a dataclass whose fields are built from `get`. Secret fields are
  kept out of `repr`.
- `agent/config.py` is the only module that reads `os.environ`.
- `compose=True` means `docker-compose.agents.yml` passes the variable;
  `compose=False` means standalone use or a per-service value.

Tests enforce the schema:

- `agent/tests/test_config_schema.py`: no `os.environ` outside `config.py`;
  `.env.example` lists exactly the declared names (plus an explicit
  `ENV_EXAMPLE_OTHER` set); secrets stay masked.
- `agent/tests/test_compose_env.py`: the variables compose passes equal the
  `compose=True` settings plus an explicit `COMPOSE_ONLY` list.

## Consequences

- Adding a setting means editing `SETTINGS`, then compose and `.env.example` as
  the tests demand. Forgetting one fails CI.
- Invalid configuration fails at startup, not mid-run.
- The schema checks types, not meaning: cross-field rules (Jev needs
  `JEV_BASE_URL`, `AGENT_BRAIN_FALLBACK` only applies to the jev brain) live in
  `Config`'s `default_factory` lambdas, not in `SETTINGS`.
- Other services keep their own env handling: `tools/wowmap/state.py` and
  `tools/agent-runner` do not use this schema.
