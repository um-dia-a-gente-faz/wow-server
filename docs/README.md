# Docs index

Start with `AGENT-DIRECTION.md`: it holds the owner's decisions and overrides older docs.
Issues live on GitHub (project 7); `UM-*` refs in these docs are the historical Linear
mirror. `scripts/check.sh test-scripts` (`scripts/tests/test_docs.py`) fails if a doc is missing here or a relative link or
backticked repo path in the docs (archived spikes: links only), `README.md`, `CLAUDE.md` or `CONTRIBUTING.md` does not resolve.

## Reference (kept current)

| Doc | Contents |
|---|---|
| [AGENT-DIRECTION.md](AGENT-DIRECTION.md) | Owner decisions for the agents: goal, autonomy, models, chat rules |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, compose projects, `agent/` module map, volumes, ports |
| [DEPLOYMENT.md](DEPLOYMENT.md) | How the VM is deployed and updated, gotchas, troubleshooting |
| [PROTOCOL-NOTES.md](PROTOCOL-NOTES.md) | 3.3.5a wire formats, each checked against TrinityCore source |
| [AGENT-API.md](AGENT-API.md) | Agent observability API (generated from `agent/api_schema.json`) |
| [AI-AGENT-SPEC.md](AI-AGENT-SPEC.md) | Original spec for the playing agents; `AGENT-DIRECTION.md` overrides it |
| [ROADMAP.md](ROADMAP.md) | Background and design notes; the tracker is the source for scope and status |
| [LIVE-MAP.md](LIVE-MAP.md) | Live map: how it was built, DBC layout, alignment |
| [CLIENT-SETUP.md](CLIENT-SETUP.md) | Game client configuration |
| [GM-COMMANDS.md](GM-COMMANDS.md) | Useful GM commands |
| [MODEL-SERVING.md](MODEL-SERVING.md) | Running the local model server for the agents' think step |

## Decisions

| Path | Contents |
|---|---|
| [adr/](adr/) | Architecture decision records, `0001` to `0010` |
| [agents/](agents/) | Config for the engineering skills: issue tracker, triage labels, domain docs |

## Runbooks

| Doc | Contents |
|---|---|
| [WOW-AGENTS-PROVISIONING.md](WOW-AGENTS-PROVISIONING.md) | Provisioning the `wow-agents` VM |
| [AGENT-RUN-1-10.md](AGENT-RUN-1-10.md) | Unattended level 1 to N evaluation run (UM-55) |
| [REPRODUCE-PROMPT.md](REPRODUCE-PROMPT.md) | Prompt to rebuild the whole environment on Proxmox (in Portuguese) |

## Spikes (archived)

Finished investigations in [spikes/](spikes/). Each starts with a status line: outcome, issue, superseded by.

| Doc | Outcome |
|---|---|
| [spikes/CHARACTER-MODEL-SPIKE.md](spikes/CHARACTER-MODEL-SPIKE.md) | go: 3D body model in the inspect drawer (#171) |
| [spikes/CHAT_FEED_SPIKE.md](spikes/CHAT_FEED_SPIKE.md) | log-tail recommendation, superseded in part by the chat relay (UM-47) |
| [spikes/HP_POWER_SPIKE.md](spikes/HP_POWER_SPIKE.md) | no-go on RA/GM commands for max HP and power (UM-46) |
| [spikes/LLM_SPIKE.md](spikes/LLM_SPIKE.md) | which free LLM decides actions (UM-61) |
| [spikes/MAP_ENGINE_SPIKE.md](spikes/MAP_ENGINE_SPIKE.md) | stay on Leaflet (UM-77); prototype in `spikes/map-engine-spike/` |
