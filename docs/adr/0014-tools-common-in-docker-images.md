# ADR 0014: tools/common reaches each Docker image through a `tools/` build context

## Status

Accepted (#451, part 2 of #263).

## Context

`tools/common` (env readers, `JsonHandler`, the agent-account rule) is shared by the tools'
servers. Docker only sees files inside the build context, and the contexts were per tool:
`tools/chat-feed` for chat-feed, `tools/` for wowmap (it already needed `tools/dbc`). The
agent image is built from the repo root and does not use `tools/`. `agent/` stays stdlib-only
and must not import `tools/`.

## Decision

Every image of a tool that imports `tools/common` builds with context `tools/` and a
Dockerfile path inside it, and copies the library next to the app:

```dockerfile
COPY common/*.py ./common/
```

`common/*.py` leaves `common/tests/` out of the image. A checkout and an image then have the
same import (`from common.env import ...`): the app puts `tools/` (checkout) or its own
directory (image) on `sys.path`. Each Dockerfile ends with an import smoke test, so a module
that is missing from the image fails the build.

Changes this PR makes to the deploy path:

- `docker-compose.yml`: chat-feed `build: ./tools/chat-feed` becomes context `./tools`,
  dockerfile `chat-feed/Dockerfile`. wowmap already used `../tools`.
- `.github/workflows/ci.yml`: the wowmap image job also builds the chat-feed image and runs
  when `tools/common` or `tools/chat-feed` change.
- `scripts/deploy.sh`: a change under `tools/common/` redeploys both the game stack and the
  monitoring stack, since both images contain it.

New tools that import `tools/common` follow the same two lines. A tool that runs from a
checkout (agent-runner, on the agent host) needs nothing: it adds `tools/` to `sys.path`.

## Alternatives rejected

- **Repo root as the context.** Every build would send the whole repo (the root
  `.dockerignore` is written for the agent image) and the two compose files and CI would all
  change. `tools/` is already wowmap's context.
- **Copy `tools/common` into each tool's directory (a build step or a committed copy).** A
  committed copy drifts; a build step is one more thing that can be forgotten, and the CI image
  build would not see it.
- **Symlinks.** Docker does not follow symlinks that leave the context.
- **A named additional build context (`additional_contexts` / `COPY --from=`).** Needs
  Compose and Buildx features on the VM and in CI that nothing else here uses, for the same
  result as widening the context by one directory.
- **A pip package or git submodule.** Nothing in `tools/` is packaged; a pip install would
  need an index or a wheel in the repo. `agent/` has no pip dependencies by rule.
- **A bind mount at run time.** Moves the dependency out of the image, so the import smoke test
  cannot catch a missing file and the image no longer runs on its own.

## Consequences

- One edit to `tools/common` rebuilds every image that copies it; `deploy.sh` and CI know.
- A tool image depends on the whole `tools/` context being sent to the Docker daemon (small:
  no art or DBC data lives there).
- The remaining servers (agent-runner, jev-mock, `agent/http_api.py`) move to `JsonHandler` in
  follow-ups. `agent/http_api.py` cannot import `tools/` at all; it keeps its own handler or
  waits for a stdlib module under `agent/`.
