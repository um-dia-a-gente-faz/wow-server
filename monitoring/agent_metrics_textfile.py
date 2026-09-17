#!/usr/bin/env python3
"""Prometheus textfile-collector exporter for agent decision-audit metrics
(UM-51).

No UM-50 `/metrics` HTTP server exists for the agent (agent/ has no
long-running process suitable for one — each container just runs the think
loop, and there's no always-on service to attach a listener to without
adding one). This follows the textfile-collector pattern instead, consistent
with `monitoring/docker-compose.yml`'s `node-exporter` (which already reads
`/host/proc`, `/host/sys` off the same VM) — one more read-only mount plus
`--collector.textfile.directory` turns node-exporter into the scrape target
for these metrics too, no new port/service to run or keep alive.

Usage (run periodically, e.g. cron or a one-shot sidecar container, on the
box where AGENT_AUDIT_DIR is mounted):

    python3 monitoring/agent_metrics_textfile.py \\
        --audit-dir /data/audit --out /var/lib/node_exporter/textfile/wow_agent.prom

Writes atomically (temp file + rename) so node-exporter's textfile
collector never reads a half-written file.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent.metrics import derive_metrics, find_audit_files, iter_records, render_prometheus_text  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--audit-dir", default=os.environ.get("AGENT_AUDIT_DIR", "/data/audit"))
    p.add_argument("--out", default="/var/lib/node_exporter/textfile/wow_agent.prom")
    args = p.parse_args(argv)

    files = find_audit_files(args.audit_dir)
    records = []
    for path in files:
        records.extend(iter_records(path))

    by_agent = derive_metrics(records)
    text = render_prometheus_text(by_agent)

    tmp = args.out + ".tmp"
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, args.out)
    print(f"wrote {len(by_agent)} agent(s), {len(records)} record(s) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
