#!/usr/bin/env python3
"""Offline A/B replay for recorded agent snapshots (stdlib only).

Run with ``python3 -m agent.tools.ab audit.jsonl --brains llm,jev``. Only
records carrying a full snapshot can be replayed; audit logs intentionally
omit most snapshots, retaining just their hash.
"""
import argparse
import hashlib
import json
import time

from agent import actions, candidates
from agent.config import Config
from agent.jev import JevClient
from agent.llm import LLMClient, LLMError
from agent.tools.replay import load_records


class FixtureJev:
    """Offline Jev adapter: match recorded request inputs, return real output."""
    def __init__(self, path):
        self.entries = {}
        self.last_usage, self.last_latency_ms, self.last_confidence = {}, None, None
        for rec in load_records(path):
            self.entries[rec["input_hash"]] = rec
        self.model = "recorded-fixture"

    @staticmethod
    def input_hash(snapshot, options):
        raw = json.dumps({"snapshot": snapshot, "candidates": options}, sort_keys=True,
                         separators=(",", ":"), default=str).encode()
        return hashlib.sha256(raw).hexdigest()

    def choose_action(self, snapshot, options, persona="", history=None):
        key = self.input_hash(snapshot, options)
        rec = self.entries.get(key)
        if rec is None:
            raise ValueError("no Jev fixture for this snapshot/candidate input")
        self.last_usage = rec.get("usage", {})
        self.last_latency_ms = rec.get("latency_ms")
        self.last_confidence = rec.get("confidence")
        return rec["action"], rec.get("params", {})


def _action_key(action, params):
    return json.dumps([action, params or {}], sort_keys=True, separators=(",", ":"), default=str)


def _percentile(values, p):
    if not values:
        return None
    vals = sorted(values)
    return vals[round((len(vals) - 1) * p)]


def run(paths, brains, from_cycle=None, limit=None, max_calls=100, estimated_cost=0.0,
        record_path=None, fixture_path=None, output=None):
    cfg = Config()
    records = []
    for path in paths:
        records.extend((path, r) for r in load_records(path))
    records = [(p, r) for p, r in records if (from_cycle is None or (r.get("cycle") or 0) >= from_cycle)]
    if limit is not None:
        records = records[:limit]
    replayable = [(p, r) for p, r in records if isinstance(r.get("snapshot"), dict)]
    calls = 0
    for _, r in replayable:
        options = candidates.generate(r["snapshot"], reflex_state=r.get("reflex") or {})
        calls += sum(1 for b in brains if b == "llm" or len(options) > 1)
    if calls > max_calls:
        raise ValueError(f"refusing: {calls} provider calls projected exceeds --max-calls {max_calls}")
    print(f"Projected provider calls: {calls}; estimated cost: ${calls * estimated_cost:.4f}")
    if "llm" in brains and (not cfg.llm_base_url or not cfg.llm_model):
        raise ValueError("LLM_BASE_URL and LLM_MODEL are required for --brains llm")
    if "jev" in brains and not fixture_path and not cfg.jev_api_key:
        raise ValueError("JEV_API_KEY (or OPENROUTER_API_KEY) required for live Jev")
    clients = {}
    if "llm" in brains:
        clients["llm"] = LLMClient(cfg.llm_base_url, cfg.llm_model, cfg.llm_api_key)
    if "jev" in brains:
        clients["jev"] = FixtureJev(fixture_path) if fixture_path else JevClient(
            cfg.jev_base_url, cfg.jev_model, cfg.jev_api_key, path=cfg.jev_path)
    fixture_out = open(record_path, "a", encoding="utf8") if record_path else None
    rows = []
    for path, rec in records:
        snap = rec.get("snapshot")
        if not isinstance(snap, dict):
            print(f"skip {path} cycle={rec.get('cycle')}: snapshot omitted (hash only)")
            continue
        opts = candidates.generate(snap, reflex_state=rec.get("reflex") or {})
        row = {"source": path, "cycle": rec.get("cycle"), "snapshot_hash": rec.get("snapshot_hash"),
               "recorded": rec.get("tool_call") or {}, "brains": {}}
        for name in brains:
            client = clients[name]
            started = time.monotonic()
            try:
                if name == "jev":
                    captured = []
                    if fixture_out:
                        # Capture real response while preserving JevClient's validation.
                        original_post = client._post
                        def capture(postpath, body):
                            response = original_post(postpath, body)
                            captured.append(response)
                            return response
                        client._post = capture
                    chosen = client.choose_action(snap, opts, persona=rec.get("goal") or "")
                    if fixture_out:
                        client._post = original_post
                        if captured:
                            response = captured[0]
                            fixture_out.write(json.dumps({"input_hash": FixtureJev.input_hash(snap, opts),
                                "action": chosen[0], "params": chosen[1], "confidence":
                                response.get("answers", {}).get("next_action", {}).get("confidence"),
                                "usage": response.get("usage", {}), "latency_ms": client.last_latency_ms}) + "\n")
                            fixture_out.flush()
                else:
                    chosen = client.choose_action(snap, actions.catalog(), persona=rec.get("goal") or "")
                latency = getattr(client, "last_latency_ms", None)
                if latency is None:
                    latency = (time.monotonic() - started) * 1000
                action, params = chosen
                item = {"action": action, "params": params, "valid": action in actions.REGISTRY,
                        "differs_from_recorded": _action_key(action, params) != _action_key(
                            row["recorded"].get("name"), row["recorded"].get("args")),
                        "latency_ms": latency, "confidence": getattr(client, "last_confidence", None),
                        "usage": getattr(client, "last_usage", {}) or {},
                        "cost": (getattr(client, "last_usage", {}) or {}).get("cost", 0)}
            except (ValueError, LLMError, Exception) as exc:
                item = {"error": str(exc), "valid": False, "latency_ms": getattr(client, "last_latency_ms", None),
                        "usage": getattr(client, "last_usage", {}) or {}, "cost": 0}
            row["brains"][name] = item
        rows.append(row)
        print(f"cycle={row['cycle']} " + " ".join(f"{n}={row['brains'][n].get('action', 'ERROR')}" for n in brains))
    if fixture_out:
        fixture_out.close()
    summaries = {}
    for name in brains:
        selected = [r["brains"][name] for r in rows]
        lat = [x["latency_ms"] for x in selected if isinstance(x.get("latency_ms"), (int, float))]
        summaries[name] = {"valid_choice_rate": sum(bool(x.get("valid")) for x in selected) / len(selected) if selected else 0,
            "distinct_actions": sorted({x["action"] for x in selected if x.get("action")}),
            "p50_latency_ms": _percentile(lat, .5), "p95_latency_ms": _percentile(lat, .95),
            "total_cost": sum(x.get("cost", 0) or 0 for x in selected)}
    if len(brains) == 2:
        agreements = [len({r["brains"][n].get("action") for n in brains}) == 1 for r in rows]
        for summary in summaries.values():
            summary["agreement_rate"] = sum(agreements) / len(agreements) if agreements else 0
    artifact = {"brains": brains, "input_files": paths, "selected": len(records), "replayed": len(rows),
                "skipped_without_snapshot": len(records) - len(rows), "summary": summaries, "cycles": rows}
    if output:
        with open(output, "w", encoding="utf8") as f:
            json.dump(artifact, f, indent=2, sort_keys=True)
            f.write("\n")
    print(json.dumps(summaries, indent=2, sort_keys=True))
    return artifact


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("files", nargs="+")
    p.add_argument("--from", dest="from_cycle", type=int)
    p.add_argument("--limit", type=int)
    p.add_argument("--brains", default="llm,jev")
    p.add_argument("--max-calls", type=int, default=100)
    p.add_argument("--estimated-cost-per-call", type=float, default=0.0)
    p.add_argument("--record", metavar="JSONL", help="append real Jev decisions as input-matched fixtures")
    p.add_argument("--fixtures", help="replay Jev decisions from recorded fixtures, no network")
    p.add_argument("--output", default="ab-results.json")
    a = p.parse_args(argv)
    brains = [b.strip() for b in a.brains.split(",") if b.strip()]
    if not brains or any(b not in ("llm", "jev") for b in brains):
        p.error("--brains must contain llm and/or jev")
    try:
        run(a.files, brains, a.from_cycle, a.limit, a.max_calls, a.estimated_cost_per_call,
            a.record, a.fixtures, a.output)
    except (ValueError, OSError) as e:
        p.error(str(e))


if __name__ == "__main__":
    main()
