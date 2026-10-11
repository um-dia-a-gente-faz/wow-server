# ADR 0013: Admin-only GM control from the site

## Status

Proposed (#311), docs only. Amends the `CLAUDE.md` rule "Never use GM commands on agent
characters" and the matching note in `docs/AGENT-DIRECTION.md`. **The owner approves those
two amendments explicitly before this PR merges.** Open questions are collected at the end.

ADR numbers: `0011` is taken on `main` (agent memory store). Issue #311 calls this ADR 0012.

## Context

The rule in `CLAUDE.md` forbids GM commands (`.go`, `.die`, `.modify`, ...) on agent
characters. It exists so that coding sessions and agents do not move or alter characters
behind the owner's back, and because a same-map teleport does not finish server-side
without `MSG_MOVE_TELEPORT_ACK` (`docs/AGENT-DIRECTION.md`, "Known findings").

Console v4 (spec #310) gives the owner a site to run the agents from. For debugging, the
owner wants a few GM-level actions there: bring a dead agent back, fix its health, move it
next to a player. That contradicts the rule as written. Spec #310 says this is raised
explicitly and handled by an ADR plus an owner-approved amendment, not by silently
overriding the rule.

## Decision

Two tiers of control, both admin-only (an admin session; see ADR 0012 for roles):

- **Tier 1** is ordinary player actions delegated to the agent through its API: set mission,
  pause and resume the brain, walk to a point, follow or stop following, start and stop
  agent containers, edit the roster, create agent accounts through the agent runner. Bounded
  by the least-privilege control-plane work already planned. Tier 1 is not GM control and
  is outside the amended rule.
- **Tier 2** is GM-level control, for debugging only: revive, heal, teleport, summon to a
  player. This is the exception to the rule.

Tier 2 rules:

- **Who:** only an Admin with a valid session. Viewers cannot reach it.
- **How it is issued:** by the site's backend, through the server's command channel. Never
  by agent code. Agents never have access to Tier 2 commands, and no code under `agent/`
  gains one.
- **On which characters:** agent characters.
- **Confirm-gated:** every Tier 2 action needs an explicit confirm click, and Tier 2 sits
  in a separate debug area of the site so it is not used by accident.
- **Logged:** every Tier 1 and Tier 2 action is recorded in the site's own store with who,
  when, what and the result.
- **Teleport is disabled** until the teleport-acknowledge handling (`MSG_MOVE_TELEPORT_ACK`)
  exists, so a same-map teleport cannot leave an agent half-moved.
- **Override.** An override pauses the agent's brain and reflexes until the admin releases
  it; then the agent resumes its mission. An agent under override is clearly marked.

Still forbidden, from the site and for every actor:

- Controlling the worldserver (no restart) or the game database.
- Touching the owner's account or character (Rubens), or any account except creating
  agent accounts through the agent runner (Tier 1).
- GM commands by an agent, by agent code, or by a coding or loop session. The `CLAUDE.md`
  rule stays for them.

## Amendments

`CLAUDE.md`, "Never": the bullet keeps its text and gains one sentence saying the only
exception is an admin acting through the site's audited Tier 2 control (this ADR).
`docs/AGENT-DIRECTION.md`, "Known findings": the teleport-ack bullet gains the same
exception and a note that site teleport stays disabled until the acknowledge exists.
The exact wording is in the PR and needs the owner's approval.

## Consequences

- The site's backend holds a credential for the server's command channel. Where it lives and
  how it is scoped is an open question; it must stay in `.env` on the VM and never in the
  repo.
- The action log is the audit trail. A Tier 2 action with no log row is a bug.
- The skills and agent briefs that restate the rule (`.claude/skills/live-agent-test`,
  `.claude/agents/loop-author.md`, `.claude/agents/loop-reviewer.md`,
  `.claude/skills/milestone-loop`) stay as they are: for them the rule is unchanged.

## Open questions

1. Exactly which commands each tier contains. #310 lists "revive, heal, teleport, summon to
   a player" for Tier 2 and names no command text (for example which GM command implements
   heal), nor whether each takes parameters.
2. The server's command channel: which interface the backend uses (for example the remote
   access or SOAP interface) and which account and GM level it runs as. #310 says only "the
   server's command channel".
3. How an admin is authenticated for Tier 2 beyond the session: #310 requires an admin
   session and a confirm click, and does not say whether a second factor or re-entering the
   password is wanted.
4. Whether Tier 2 may target any character on the realm or only characters in the agent
   roster. #310 says the owner's account is out of reach; it does not say how the target is
   restricted for other players' characters.
5. Retention of the action log, and whether it is append-only at the database level.
6. Teleport also depends on the teleport-acknowledge fix, which has no issue named in #310.
