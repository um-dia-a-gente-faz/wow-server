# ADR 0001: Jev as the agent's decision brain (opt-in), chat deferred

## Status

Proposed

## Context

`agent/think.py` picks one action per cycle via `LLMClient.choose_action()`
(UM-44), an OpenAI-compatible tool-calling call to a free model
(`docs/AGENT-DIRECTION.md` §3). This has been a persistent source of
failures: `auto` routing measured ~50% valid tool-call rate
(`docs/LLM_SPIKE.md`), and even pinned free models still fail to call tools
reliably enough to clear UM-44's ≥90% bar without heavy validation
scaffolding — UM-89 (64-bit GUIDs mangled by a float round-trip through the
model's own text), UM-92 (unvalidated free-text chat/name arguments reaching
the game), and UM-94 (per-model fallback/backoff for a shared free-tier
key), all filed against live-run failures in Milestone 2.

Jev (TypeSafe's "System One" decision model) answers a narrower question
instead: given application state and a list of options you supply, which
one, with a calibrated confidence — no free text, no argument generation.
It is available through OpenRouter as `typesafe/jev-1.13` (alias
`~typesafe/jev-latest`), authenticated with a plain OpenRouter API key (no
separate TypeSafe account), via `POST https://openrouter.ai/api/alpha/decisions`
or the TypeSafe SDK pointed at `POST https://openrouter.ai/api/v1/systemone`.
Because the output is constrained to the options handed to it, it cannot
hallucinate an action name or emit malformed params — the entire problem
class UM-89/UM-92 exist to patch around disappears by construction. It
cannot generate chat text, explanations, or persona dialogue at all.

The project's near-term goal (owner, 2026-09-25) is scaling from 1 agent
(level 1→10) to a party of 5 to a 25-agent raid roster (UM-102). Reliable,
cheap, low-latency tactical decisions matter far more at that scale than
chat/roleplay does right now.

## Decision

Jev becomes the decision-maker (selected per agent with `AGENT_BRAIN=jev`; the
default stays `llm`, see the 2026-10-04 update below) for the agent's existing tactical
action set: combat, target, loot, quest accept/turn-in/abandon, movement,
party-accept, and follow/assist. A candidate generator (UM-97) enumerates
concrete `(action, params)` options from `world.snapshot()` each think
cycle; Jev picks one via its `Choice` primitive. Outgoing chat-generation
actions (`say`/`yell`/`whisper`/`emote`/`channel_say`) are removed from the
action registry for now (UM-98) — not dropped. Party-accept, follow, and
assist stay: they need no free text (accepting an invite is a plain
yes/no decision), so only agent-*initiated* social grouping (UM-62) is
deferred, not the party mechanics Milestone 3 already needs.

`LLMClient` stays wired behind the same brain interface (UM-101) as an
explicit fallback and future roleplay layer, not deleted —
`docs/AGENT-DIRECTION.md` §3 already anticipated "paid models may be added;
keep the provider behind an interface." This decision overrides that
section's "free only" for Jev specifically: cost is input-token-only
(`$0.042`/M tokens at the time of writing), output tokens are free, and
every response reports its own cost via `usage.cost`. Expected spend at
current agent counts (1-5 agents, 10-30s think interval) is well under
$5/month; revisit the ceiling explicitly before scaling toward the
25-agent raid roster (UM-63/UM-102).

Development against real Jev is blocked on provisioning `OPENROUTER_API_KEY`
into the agent containers. A local mock (`tools/jev-mock/`, UM-96) stands in
until then: a stdlib-only HTTP server implementing the same Decisions API
request/response shape, with a deliberately unsophisticated policy (pick a
uniformly random candidate) — precision doesn't matter for it, only
exercising the real HTTP contract does. `jev-mock` is not a throwaway: it
stays in dev `docker-compose` and CI permanently (UM-100) as the fast, free
path. Real Jev is manual/local verification only, the same tier as live-VM
checks in `CONTRIBUTING.md` — it never becomes a required CI dependency.

Milestone 3 is renamed from "Social agents" to "Jev decision brain" and
rescoped to this work plus the existing 25-agent roster scale-out (UM-63).
UM-62 (agents noticing each other and negotiating groups through chat) moves
to "Later — autonomy & multi-agent," deferred, not dropped.

## Consequences

- Tool-call-format failures (the whole premise of UM-89/UM-92) stop being a
  live problem class; those PRs are closed as superseded rather than
  extended. UM-94 (LLM fallback routing) is shelved, not closed — it
  matters again once the LLM is back in active use as fallback/roleplay.
- The agent cannot chat, whisper (as a *conversation*), emote, or express
  persona until the LLM roleplay layer is revisited — a real behavioral
  regression from Milestone 1's "says so in party chat" demo, accepted
  deliberately here in exchange for reliability at scale.
- Decision quality now depends on the candidate generator's coverage, not
  on prompt engineering: failures move from "the model picked badly" to
  "the generator didn't offer the right option," which is unit-testable in
  a way free-text tool calls weren't (UM-97's acceptance criteria).
- New paid, internet-dependent, non-self-hostable component in the play
  loop. Mitigated by (a) keeping it behind the same interface as the free
  LLM fallback (UM-101), and (b) never making required CI depend on the
  real API (UM-100).
- Party mechanics (accept/follow/assist) are unaffected — the 5-agent party
  milestone does not need to wait on any social/chat work.

## Alternatives considered

- **Cheap LLM proposes candidates, Jev ranks them.** Rejected: the LLM
  still generates the tokens (that's the cost and the failure surface),
  and Jev only re-ranks what it's handed — candidate quality stays the
  LLM's problem, not Jev's, while adding a second network hop.
- **Keep the LLM as primary, add Jev nowhere.** Rejected: doesn't address
  the ≥90% tool-call reliability bar that's blocked Milestone 2/3 progress
  and produced UM-89/UM-92/UM-94.
- **Self-host a decision model.** Rejected: Jev isn't open-weight or
  self-hostable; no local equivalent exists today. Revisit if OpenRouter
  pricing or availability ever becomes a blocker.

## Update (GH-165): confidence policy

`Brain.decide()` (agent/brain.py) is the single place Jev's confidence is
used. `JEV_MIN_CONFIDENCE` (default `0.0` = off until chosen from measured
data): `confidence >= threshold` acts as chosen; below it the generator's
`idle` candidate runs instead and the audit records
`confidence_rule=low_confidence_safe_fallback`, `confidence_threshold`,
`confidence` and `overridden` (the candidate Jev chose). No confidence
reported is `confidence_unknown`: acted as chosen, counted separately. The
rule never calls the LLM.

## Update 2026-10-04: peer brains, no automatic fallback (#161)

The owner's decision supersedes the "Jev primary, LLM as per-cycle fallback"
framing above (`docs/AGENT-DIRECTION.md` still overrides this ADR):

- **The LLM and Jev are peer brains.** Each agent is played by exactly one,
  chosen with `AGENT_BRAIN=llm|jev` (default `llm`, so nothing changes until
  asked for). Jev is opt-in: a Jev key/URL alone does not select it, and the
  agent logs a warning when Jev is configured but `AGENT_BRAIN` is not `jev`.
  The LLM path stays first-class and plays exactly as before;
  with `AGENT_BRAIN=llm` no Jev client is ever constructed.
- **No silent substitution.** With `AGENT_BRAIN=jev`, a Jev error (including a
  cooldown skip) is a failed cycle: audited as `brain=jev` with the reason in
  `fallback`, and no LLM call that cycle.
- **Fallback is explicit and off by default.** `AGENT_BRAIN_FALLBACK=llm` lets
  the LLM decide when Jev fails. The audit row then has `brain=llm` and
  `substituted=true`, so "jev decided", "llm decided" and "llm substituted for
  jev" are distinguishable.

If the agent-host ADR (0002 is taken; its in-review successor) merges first and
this update is split out, it becomes ADR 0003 and says so.
